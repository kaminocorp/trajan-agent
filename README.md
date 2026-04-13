<h1 align="center">Trajan Agent</h1>

<p align="center">
  Connect your GitHub repos. Get AI-generated docs, commit-based progress tracking, and an agent that understands your codebase — no tickets, no manual updates.
</p>

<p align="center">
  <a href="https://www.trajancloud.com">Website</a>&nbsp;&nbsp;·&nbsp;&nbsp;<a href="https://www.trajancloud.com/docs">API Docs</a>&nbsp;&nbsp;·&nbsp;&nbsp;<a href="https://github.com/kaminocorp/trajan-agent/issues">Issues</a>
</p>

<p align="center">
  <strong>Python 3.11+ &nbsp;·&nbsp; FastAPI &nbsp;·&nbsp; SQLModel &nbsp;·&nbsp; PostgreSQL &nbsp;·&nbsp; Claude AI</strong>
</p>

---

## What Trajan does

You give Trajan your GitHub repositories. It reads your code and gives you:

| Capability | What happens |
|---|---|
| **AI documentation** | Generates changelogs, architecture blueprints, and implementation plans by analyzing your source code. Regenerates only when code changes. |
| **Progress tracking** | Turns commits into activity dashboards, contributor summaries, and velocity metrics. No tickets or manual status updates. |
| **PM agent** | A chat interface with full context on your repos, commits, PRs, and issues. Answers questions about your codebase. |
| **Two-way GitHub sync** | Pushes generated documents to GitHub branches and opens PRs automatically. |
| **Feedback ingestion** | Public API endpoint for collecting user feedback. AI interprets, deduplicates, and categorises submissions. |
| **Team workspaces** | Organizations with role-based access (owner / admin / member / viewer) and per-product permission overrides. |
| **Environment vault** | Encrypted storage for env vars, service URLs, and infrastructure notes — Fernet encryption at rest. |

This is the **open-source community edition** — all features unlocked, no repo limits, no paywalls.

---

## Quick start

### Prerequisites

- Python 3.11+
- A [Supabase](https://supabase.com) project (free tier works)
- An [Anthropic API key](https://console.anthropic.com) for AI features

### Setup

```bash
git clone https://github.com/kaminocorp/trajan-agent.git
cd trajan-agent

python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
# Edit .env — see "Environment variables" below
```

### Run

```bash
uvicorn app.main:app --reload --port 8000
```

The API is available at `http://localhost:8000`. Health check at `GET /health`.
Interactive docs at [`/docs`](http://localhost:8000/docs) (Swagger) and [`/redoc`](http://localhost:8000/redoc).

### Environment variables

| Variable | Required | Description |
|----------|:--------:|-------------|
| `DATABASE_URL` | Yes | Supabase pooled connection (port 6543) |
| `DATABASE_URL_DIRECT` | Yes | Supabase direct connection for migrations (port 5432) |
| `SUPABASE_URL` | Yes | Your Supabase project URL |
| `SUPABASE_ANON_KEY` | Yes | Supabase anonymous key |
| `SUPABASE_JWKS_URL` | Yes | JWKS endpoint for JWT verification |
| `SUPABASE_SERVICE_ROLE_KEY` | Yes | Service role key for admin operations |
| `ANTHROPIC_API_KEY` | — | Enables AI doc generation, progress summaries, and PM agent |
| `TOKEN_ENCRYPTION_KEY` | — | Fernet key for encrypting secrets at rest |
| `GITHUB_APP_ID` | — | GitHub App integration for org-level repo access |
| `POSTMARK_API_KEY` | — | Enables transactional emails (digests, invites) |
| `SCHEDULER_ENABLED` | — | Background jobs — auto-progress, email digests (default: `true`) |

Set `DEBUG=true` for local development — this relaxes validation and auto-creates the schema on startup.

### Database

Trajan uses Supabase PostgreSQL with Row-Level Security. In debug mode, `init_db()` creates the schema automatically. For production, use Alembic:

```bash
alembic upgrade head
```

---

## Architecture

```
app/
├── api/v1/          # FastAPI route handlers
├── api/deps/        # Auth, feature gates, product access dependencies
├── domain/          # Business logic (BaseOperations CRUD + model-specific ops)
├── models/          # SQLModel entities with mixins (UUID, Timestamp, UserOwned)
├── services/
│   ├── agent/       # PM agent (Claude-powered, tool-calling)
│   ├── docs/        # Doc generation pipeline (changelog, blueprint, plans agents)
│   ├── github/      # GitHub API — read/write ops, App auth, token resolution
│   ├── interpreter/ # AI feedback interpretation
│   ├── progress/    # Auto-progress summaries and activity tracking
│   └── email/       # Transactional email (Postmark)
├── config/          # Settings and plan configuration
├── core/            # Database, RLS, encryption, rate limiting
└── schemas/         # Pydantic response schemas
```

**Stack:** Python 3.11+ · FastAPI · SQLModel · PostgreSQL (Supabase) · Claude AI · asyncpg

### Request lifecycle

Every authenticated request follows the same path through FastAPI's dependency injection:

```
HTTP Request
     │
     ▼
 1. Extract Bearer token from Authorization header
 2. Validate JWT against Supabase JWKS (ES256, 1h cache)
 3. Auto-create User record on first API call
 4. Acquire async DB session from pool
 5. SET LOCAL app.current_user_id = '{uuid}'   ← RLS context
 6. Resolve access: org role × product override → effective role
 7. Check feature gates (plan limits → 402 if exceeded)
 8. Route handler executes, DB queries scoped by RLS
 9. Commit → SET LOCAL resets automatically → connection returned
```

### Design decisions

<details>
<summary><strong>Row-Level Security with connection pooling</strong></summary>

<br/>

The hardest part of using PostgreSQL RLS in a pooled environment is ensuring one user's session context never leaks to another request. Trajan solves this with `SET LOCAL`, which scopes the setting to the current **transaction** — not the session. When the transaction commits or rolls back, the setting disappears.

This is safe with PgBouncer's transaction pooling because each transaction gets an isolated context, even if the underlying connection is reused.

```python
# core/rls.py — the entire implementation
await session.execute(
    text(f"SET LOCAL app.current_user_id = '{user_id}'")
)
```

RLS policies reference this via a SQL helper:

```sql
CREATE FUNCTION app_user_id() RETURNS uuid AS $$
  SELECT current_setting('app.current_user_id', true)::uuid
$$ LANGUAGE sql STABLE;

CREATE POLICY select_own ON products
  FOR SELECT USING (user_id = app_user_id());
```

</details>

<details>
<summary><strong>Dual connection pools</strong></summary>

<br/>

Supabase exposes PostgreSQL through two endpoints: a transaction pooler (port 6543) for normal operations, and a direct connection (port 5432) for long-running work.

```
Transaction Pooler (6543)               Direct Connection (5432)
─────────────────────────               ────────────────────────
pool_size=10, max_overflow=20           pool_size=3, max_overflow=5
command_timeout=60s                     command_timeout=300s
statement_cache_size=0                  Prepared statements enabled

Used for: API endpoints (95%)          Used for: Doc generation,
                                       AI analysis, migrations
```

Route handlers use `Depends(get_db)` for the pooler and `Depends(get_direct_db)` for long operations.

</details>

<details>
<summary><strong>Generic domain layer</strong></summary>

<br/>

Business logic lives in operation classes that extend `BaseOperations[T]`, a generic repository providing type-safe CRUD:

```python
class BaseOperations(Generic[ModelType]):
    async def get(self, db, id) -> ModelType | None: ...
    async def get_by_user(self, db, user_id, id) -> ModelType | None: ...
    async def get_multi_by_user(self, db, user_id, skip, limit) -> list[ModelType]: ...
    async def create(self, db, obj_in, user_id) -> ModelType: ...
    async def update(self, db, db_obj, obj_in) -> ModelType: ...
    async def delete(self, db, id, user_id) -> bool: ...
```

Each model extends this with domain-specific methods. Route handlers never contain SQL or business logic — all domain logic lives in operation classes.

```python
# Module-level singleton — imported and used directly
product_ops = ProductOperations()
```

</details>

<details>
<summary><strong>Three-level access control</strong></summary>

<br/>

Access is resolved by composing organization roles with per-product overrides:

```
Organization Role     Product Override     Effective Access
─────────────────     ────────────────     ────────────────
owner                 (any or none)     →  admin
admin                 (any or none)     →  admin
member                editor            →  editor
member                viewer            →  viewer
member                none              →  none (blocked)
viewer                (any or none)     →  viewer
```

This lets an organization keep someone as a `member` broadly while restricting them to `viewer` on specific products, or blocking access entirely. Dependencies like `require_product_editor()` compose naturally in FastAPI's DI.

</details>

<details>
<summary><strong>Multi-instance scheduling</strong></summary>

<br/>

Background jobs (email digests, auto-progress) run via APScheduler inside the FastAPI process. When multiple instances are running, PostgreSQL advisory locks prevent duplicate execution:

```python
async with advisory_lock(AUTO_PROGRESS_LOCK_ID) as acquired:
    if not acquired:
        return  # Another instance already running this job
    await auto_progress_generator.run_for_all_orgs(db)
```

`pg_try_advisory_lock()` is non-blocking — the losing instance skips immediately. No Redis, no external coordinator, just PostgreSQL.

</details>

---

## Development

### Linting and type checking

```bash
ruff check . && ruff format .    # Lint + format
mypy app                          # Type checking (strict mode)
```

### Testing

```bash
pytest tests/unit/                                       # Unit tests (no DB required)
pytest tests/unit/domain/test_product_operations.py      # Single file
pytest -k "test_name"                                    # Pattern match

TRAJAN_TESTS_ENABLED=1 pytest tests/integration/         # Integration (needs Supabase)
```

### Code style

- **Line length:** 100 characters
- **Formatter:** ruff (isort, pycodestyle, pyupgrade, bugbear, simplify)
- **Type checker:** mypy strict with Pydantic plugin
- **Python:** 3.11+, 4 spaces

---

## Community edition vs hosted

This is the community edition — all features unlocked, self-hosted. The [hosted version](https://www.trajancloud.com) adds managed infrastructure, automatic updates, and tiered plans.

| | Community | Hosted |
|---|---|---|
| Features | All unlocked | Tiered plans |
| Repo limit | Unlimited | Per plan |
| Infrastructure | Self-managed | Managed |
| Updates | Pull from this repo | Automatic |

---

## Contributing

Contributions are welcome — please [open an issue](https://github.com/kaminocorp/trajan-agent/issues) first to discuss what you'd like to change.

---

## License

See [LICENSE](LICENSE) for details.
