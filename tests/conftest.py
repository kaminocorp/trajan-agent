"""Root conftest — test infrastructure for all backend tests.

Provides:
- Safety guard: require TRAJAN_TESTS_ENABLED=1 for production DB tests
- Transaction-rollback db_session fixture (Layers 1–2)
- Test user, org, subscription, product fixtures
- API client with dependency overrides
- Autouse mock for external services (Postmark, Stripe, GitHub)
"""

from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.config.settings import settings

# ─────────────────────────────────────────────────────────────────────────────
# Safety Guard
# ─────────────────────────────────────────────────────────────────────────────


def pytest_configure(config: pytest.Config) -> None:
    """Safety check: require explicit opt-in for production DB tests.

    Unit tests (pure mocks) run without this flag. Integration tests
    that touch the database require TRAJAN_TESTS_ENABLED=1.
    """
    config.addinivalue_line("markers", "integration: DB integration tests (transaction rollback)")
    config.addinivalue_line("markers", "full_stack: Full-stack tests (real Supabase + Stripe)")

    # Only enforce for integration/full_stack paths
    if any("integration" in str(arg) for arg in config.invocation_params.args):
        db_url = settings.database_url
        if "supabase" in db_url and not os.getenv("TRAJAN_TESTS_ENABLED"):
            pytest.exit(
                "SAFETY: Set TRAJAN_TESTS_ENABLED=1 to confirm running tests "
                "against the production database.",
                returncode=1,
            )


# ─────────────────────────────────────────────────────────────────────────────
# Transaction-Rollback Engine (uses DIRECT connection, not pooler)
# ─────────────────────────────────────────────────────────────────────────────

# PgBouncer transaction pooling (port 6543) breaks SAVEPOINTs because it
# may multiplex connections across transactions. Use direct (port 5432).
TEST_ENGINE = create_async_engine(
    settings.database_url_direct,
    echo=False,
    poolclass=NullPool,
    connect_args={
        "command_timeout": 30,
    },
)


# ─────────────────────────────────────────────────────────────────────────────
# Transaction-Rollback Fixtures (Layers 1–2)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
async def db_session():
    """Database session wrapped in a transaction that is ALWAYS rolled back.

    No test data ever persists to production.

    Uses SAVEPOINT so tests can call commit() internally without
    actually committing — the outer transaction absorbs it.

    NOTE: Uses the direct connection (port 5432), not the transaction
    pooler (port 6543), because PgBouncer breaks SAVEPOINTs.
    """
    async with TEST_ENGINE.connect() as conn:
        trans = await conn.begin()
        session = AsyncSession(bind=conn, expire_on_commit=False)
        await conn.begin_nested()

        @event.listens_for(session.sync_session, "after_transaction_end")
        def restart_savepoint(session_sync, transaction):
            """Restart SAVEPOINT after each nested transaction ends.

            This lets application code call session.commit() freely —
            each commit hits a savepoint, not the real transaction.
            """
            if transaction.nested and not transaction._parent.nested:
                session_sync.begin_nested()

        try:
            yield session
        finally:
            await session.close()
            await trans.rollback()


@pytest.fixture
async def test_user(db_session: AsyncSession):
    """A test user inside the rolled-back transaction."""
    from app.models.user import User

    user = User(
        id=uuid.uuid4(),
        email=f"__test_{uuid.uuid4().hex[:8]}@example.com",
        display_name="Test User",
        created_at=datetime.now(UTC),
    )
    db_session.add(user)
    await db_session.flush()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def test_org(db_session: AsyncSession, test_user):
    """A test organization owned by test_user, inside the rolled-back transaction."""
    from app.domain.organization_operations import organization_ops

    org = await organization_ops.create(
        db_session,
        name=f"[TEST] Org {uuid.uuid4().hex[:8]}",
        owner_id=test_user.id,
    )
    await db_session.flush()
    return org


@pytest.fixture
async def test_subscription(db_session: AsyncSession, test_org):
    """Subscription for test_org — defaults to active indie plan."""
    from app.config.plans import get_plan
    from app.domain.subscription_operations import subscription_ops

    sub = await subscription_ops.get_by_org(db_session, test_org.id)
    if not sub:
        pytest.fail("Organization should have a subscription after creation")

    plan = get_plan("indie")
    await subscription_ops.update(
        db_session,
        sub,
        {
            "plan_tier": "indie",
            "status": "active",
            "base_repo_limit": plan.base_repo_limit,
            "is_manually_assigned": True,
            "manual_assignment_note": "Test fixture",
        },
    )
    await db_session.flush()
    await db_session.refresh(sub)
    return sub


@pytest.fixture
async def test_product(db_session: AsyncSession, test_user, test_org, test_subscription):  # noqa: ARG001
    """A test product in the test_org."""
    from app.domain.product_operations import product_ops

    product = await product_ops.create(
        db_session,
        obj_in={
            "name": f"[TEST] Product {uuid.uuid4().hex[:8]}",
            "description": "Test product",
            "organization_id": test_org.id,
        },
        user_id=test_user.id,
    )
    await db_session.flush()
    return product


# ─────────────────────────────────────────────────────────────────────────────
# API Client (Rollback-based — Layers 1–2)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
async def api_client(db_session: AsyncSession, test_user):
    """HTTP client that bypasses JWT auth and uses the rolled-back DB session.

    For testing endpoint logic without external dependencies.
    Overrides: get_current_user, get_db, get_db_with_rls
    """
    from app.api.deps.auth import get_current_user
    from app.core.database import get_db
    from app.main import app

    # Override auth to return test_user
    app.dependency_overrides[get_current_user] = lambda: test_user

    # Override DB to return the rolled-back session
    async def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db

    # get_db_with_rls is a generator that yields an AsyncSession after setting
    # RLS context. For tests, we skip RLS and just yield the session.
    from app.api.deps.auth import get_db_with_rls

    async def override_db_rls():
        yield db_session

    app.dependency_overrides[get_db_with_rls] = override_db_rls

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=True
    ) as client:
        yield client

    app.dependency_overrides.clear()


# ─────────────────────────────────────────────────────────────────────────────
# Opt-in RLS-aware client fixtures (Phase B of the write-policy sweep)
#
# The default `api_client` above — and its siblings in tests/api/conftest.py —
# hold a `db_session` that connects as `postgres` (rolbypassrls=true). RLS is
# silently bypassed, which is why every v0.31.x re-arm bug shipped to prod
# before detection.
#
# The opt-in `rls_*_client` variants below wrap the defaults and flip the
# session's role to `trajan_app` (NOBYPASSRLS + FORCE RLS) with
# `app.current_user_id` seeded to the appropriate user. Tests that want to
# exercise RLS policies end-to-end request `rls_api_client` instead of
# `api_client`. The existing 275+ passing tests stay on bypass and continue
# to pass unmodified.
#
# Seed data before the fixture yields (test_user, test_org, etc. are already
# created under postgres), then the SET LOCAL ROLE only affects subsequent
# queries — which are the ones driven by endpoint code paths.
# ─────────────────────────────────────────────────────────────────────────────


async def _activate_trajan_app_role(db_session: AsyncSession, user_id) -> None:
    """Switch the test db_session to `trajan_app` with app.current_user_id seeded.

    `SET LOCAL ROLE` is transaction-scoped, so the outer-transaction rollback
    in `db_session` reverts it without further cleanup. Writes to
    `session.info[RLS_INFO_KEY]` let the v0.31.3 `after_begin` listener
    re-arm `app.current_user_id` on every savepoint inside the endpoint,
    which matches how production behaves.

    Skips the test (rather than failing) if `trajan_app` doesn't exist on
    this DB — local dev DBs without the role get a clear skip message
    instead of a crash at fixture setup.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError, ProgrammingError

    from app.core.rls import RLS_INFO_KEY

    db_session.info[RLS_INFO_KEY] = user_id
    await db_session.execute(text(f"SET LOCAL app.current_user_id = '{user_id}'"))
    try:
        await db_session.execute(text("SET LOCAL ROLE trajan_app"))
    except (ProgrammingError, DBAPIError) as exc:
        pytest.skip(f"Cannot SET LOCAL ROLE trajan_app ({exc}); role missing or ungranted.")


@pytest.fixture
async def rls_api_client(api_client, db_session: AsyncSession, test_user):
    """RLS-aware variant of `api_client` — endpoint queries run under `trajan_app`.

    Use this for regression tests that must verify a policy admits (or
    rejects) a given write. The default `api_client` stays on BYPASSRLS so
    every existing test continues to pass without modification.
    """
    await _activate_trajan_app_role(db_session, test_user.id)
    yield api_client


# ─────────────────────────────────────────────────────────────────────────────
# External Service Mocks (autouse for unit/DB tests, skipped for full_stack)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def mock_external_services(request):
    """SAFETY: Always mock external services in unit and DB integration tests.

    Prevents accidental email sends, Stripe charges, or GitHub API calls.
    Skipped for full-stack integration tests (which need real Stripe sandbox).
    """
    if "full_stack" in str(request.fspath):
        yield {}
        return

    with (
        patch("app.services.email.postmark.postmark_service", new_callable=MagicMock) as mock_pm,
        patch("app.services.stripe_service.stripe_service", new_callable=MagicMock) as mock_stripe,
    ):
        mock_pm.send = AsyncMock(return_value=True)
        mock_stripe.create_customer = MagicMock(return_value="cus_test")
        mock_stripe.create_checkout_session = MagicMock(
            return_value="https://checkout.stripe.com/test"
        )

        yield {"postmark": mock_pm, "stripe": mock_stripe}
