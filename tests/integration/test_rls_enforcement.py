"""RLS enforcement regression tests.

These tests were authored alongside the 0.31.0 (`trajan_app` role cutover +
FORCE ROW LEVEL SECURITY) and 0.31.1 (`users` permissive SELECT for
teammates) completions. They cover three classes of silent failure that the
existing integration suite could not catch because it runs as the `postgres`
role (rolbypassrls = true):

  1. A role/schema regression that re-enables BYPASSRLS or leaves FORCE off.
  2. The `users` table being too narrow for teammate-visible UI surfaces
     (the 0.31.1 bug, which rendered `?` placeholders in prod).
  3. Write paths that the auth layer relies on but that currently have no
     permitting RLS policy (the `users` INSERT fallback flagged as a pending
     follow-up in the 0.31.0 completion doc).

The tests seed data as `postgres` (transaction rollback, nothing persists),
then switch the session role to `trajan_app` via `SET LOCAL ROLE` so the
policies are actually evaluated before the assertions. If `trajan_app`
does not exist on the test DB (expected on fresh local envs), every
role-scoped test skips with a clear message pointing at the DBA step.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import cron_session_maker
from app.core.rls import clear_rls_context, get_current_rls_user_id, set_rls_user_context
from app.domain.organization_operations import organization_ops
from app.models.organization import MemberRole, OrganizationMember
from app.models.user import User

# ─────────────────────────────────────────────────────────────────────────────
# Local fixtures — kept in this file so the RLS suite is self-contained and
# does not entangle with the domain-test conftest.
# ─────────────────────────────────────────────────────────────────────────────


async def _make_user(db: AsyncSession, *, label: str) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"__test_{label}_{uuid.uuid4().hex[:8]}@example.com",
        display_name=f"Test {label.title()}",
        created_at=datetime.now(UTC),
    )
    db.add(user)
    await db.flush()
    await db.refresh(user)
    return user


async def _add_member(
    db: AsyncSession, *, org_id: uuid.UUID, user_id: uuid.UUID, inviter_id: uuid.UUID
) -> OrganizationMember:
    member = OrganizationMember(
        organization_id=org_id,
        user_id=user_id,
        role=MemberRole.MEMBER.value,
        invited_by=inviter_id,
        invited_at=datetime.now(UTC),
        joined_at=datetime.now(UTC),
    )
    db.add(member)
    await db.flush()
    await db.refresh(member)
    return member


# ─────────────────────────────────────────────────────────────────────────────
# TestRlsIsActuallyEnforced
#
# Catches regressions to the 0.31.0 guarantees: trajan_app NOBYPASSRLS, every
# RLS-enabled table FORCEd, and RLS actually filters rows under the app role.
# ─────────────────────────────────────────────────────────────────────────────


class TestRlsIsActuallyEnforced:
    """The preconditions that make every other RLS policy meaningful."""

    @pytest.mark.asyncio
    async def test_trajan_app_has_no_bypass_flag(self, db_session: AsyncSession):
        """`ALTER ROLE trajan_app BYPASSRLS` would silently undo the entire 0.31.0
        cutover. Assert the flag directly so that change shows up as a test
        failure, not a prod incident."""
        result = await db_session.execute(
            text("SELECT rolbypassrls FROM pg_roles WHERE rolname = 'trajan_app'")
        )
        row = result.scalar_one_or_none()
        if row is None:
            pytest.skip(
                "trajan_app role missing on this DB — apply the per-environment "
                "DBA step from docs/completions/app-role-privilege-separation.md."
            )
        assert row is False, "trajan_app must have rolbypassrls=false for RLS to evaluate"

    @pytest.mark.asyncio
    async def test_trajan_cron_connects_with_bypassrls(self):
        """The bypass-then-scope pattern (cron-role plan, phases 1–3) depends
        on `cron_session_maker` actually connecting as a role with
        `rolbypassrls=true`. If `DATABASE_URL_CRON` were ever rewired to
        a NOBYPASSRLS role (copy-paste of `DATABASE_URL`, bad Fly secret,
        etc.) every cron bootstrap query and every API-key validation
        would silently return zero rows — identical symptom to the
        pre-Phase-2 state we just recovered from.

        This test exercises the full chain (env var → settings → engine
        → pool → asyncpg auth → Supabase pooler → role identity) rather
        than just checking `pg_roles`, because the failure mode is a
        misconfigured DSN, not a missing role.
        """
        try:
            async with cron_session_maker() as cron_db:
                result = await cron_db.execute(
                    text(
                        "SELECT current_user, rolbypassrls "
                        "FROM pg_roles WHERE rolname = current_user"
                    )
                )
                row = result.one_or_none()
        except (DBAPIError, OperationalError) as exc:
            pytest.skip(
                f"cron_session_maker could not connect ({exc}); "
                "DATABASE_URL_CRON is likely a placeholder on this env. "
                "See docs/completions/cron-role-phase-1-plumbing.md."
            )

        assert row is not None, "cron role not present in pg_roles"
        current_user, rolbypassrls = row
        assert rolbypassrls is True, (
            f"cron_session_maker connected as {current_user!r} which has "
            "rolbypassrls=false. Bypass-then-scope assumes BYPASSRLS on "
            "the bootstrap session — a NOBYPASSRLS role here would make "
            "every cron job and every API-key validation silently return "
            "zero rows."
        )

    @pytest.mark.asyncio
    async def test_every_rls_enabled_table_has_force_on(self, db_session: AsyncSession):
        """A future migration that enables RLS on a new table but forgets
        `FORCE ROW LEVEL SECURITY` would only be protected against non-owner
        roles. Under `trajan_app` (NOT the owner) it'd still be protected, so
        today this is belt-and-braces — but it also catches a migration that
        accidentally disables FORCE on an existing table."""
        result = await db_session.execute(
            text(
                """
                SELECT relname
                FROM pg_class
                WHERE relnamespace = 'public'::regnamespace
                  AND relkind = 'r'
                  AND relrowsecurity = true
                  AND relforcerowsecurity = false
                """
            )
        )
        un_forced = [row[0] for row in result.all()]
        assert un_forced == [], (
            f"Tables with RLS enabled but FORCE off: {un_forced}. "
            "Every RLS-enabled table must be FORCEd so the owner role is "
            "also subject to policies (see 0.31.0)."
        )

    @pytest.mark.asyncio
    async def test_rls_filters_when_no_user_context(
        self,
        db_session: AsyncSession,
        trajan_app_available: bool,
        as_trajan_app,
    ):
        """With `trajan_app` active and NO `app.current_user_id` set,
        `app_user_id()` returns NULL and every user-scoped policy evaluates
        to FALSE. A SELECT on an RLS-protected table should return zero
        rows. This is the canonical "RLS is actually on" smoke test."""
        if not trajan_app_available:
            pytest.skip("trajan_app role not present on this DB.")

        # Seed something (an org) as postgres so there is a row to hide.
        user = await _make_user(db_session, label="owner")
        await organization_ops.create(db_session, name="[TEST] RLS Org", owner_id=user.id)
        await db_session.flush()

        # Deliberately do not set app.current_user_id. Clear any carry-over.
        await clear_rls_context(db_session)

        async with as_trajan_app(db_session):
            result = await db_session.execute(text("SELECT count(*) FROM organizations"))
            visible = result.scalar_one()

        assert visible == 0, (
            f"Expected RLS to filter all rows with no user context; got {visible}. "
            "This means trajan_app is bypassing RLS or a policy is permissive-all."
        )


# ─────────────────────────────────────────────────────────────────────────────
# TestUsersTeammateVisibility
#
# The direct regression test for 0.31.1. Exercises both the new
# `shares_org_with` helper and the `users_select_org_members` permissive
# policy, plus the two API surfaces that broke in prod.
# ─────────────────────────────────────────────────────────────────────────────


class TestUsersTeammateVisibility:
    """0.31.1 regression: cross-user reads of the `users` table must return
    full rows for teammates and zero rows for strangers."""

    @pytest.mark.asyncio
    async def test_user_reads_own_row(
        self,
        db_session: AsyncSession,
        trajan_app_available: bool,
        as_trajan_app,
    ):
        """`users_select_own` (pre-0.31.1) — a fresh user with no org
        memberships still reads their own row."""
        if not trajan_app_available:
            pytest.skip("trajan_app role not present on this DB.")

        user = await _make_user(db_session, label="solo")
        await db_session.flush()

        await set_rls_user_context(db_session, user.id)
        async with as_trajan_app(db_session):
            result = await db_session.execute(
                text("SELECT id FROM users WHERE id = :id"), {"id": user.id}
            )
            assert result.scalar_one_or_none() == user.id

    @pytest.mark.asyncio
    async def test_user_reads_teammate_row(
        self,
        db_session: AsyncSession,
        trajan_app_available: bool,
        as_trajan_app,
    ):
        """`users_select_org_members` (0.31.1) — teammate rows are visible."""
        if not trajan_app_available:
            pytest.skip("trajan_app role not present on this DB.")

        reader = await _make_user(db_session, label="reader")
        teammate = await _make_user(db_session, label="teammate")
        org = await organization_ops.create(
            db_session, name="[TEST] Teammate Org", owner_id=reader.id
        )
        await _add_member(db_session, org_id=org.id, user_id=teammate.id, inviter_id=reader.id)
        await db_session.flush()

        await set_rls_user_context(db_session, reader.id)
        async with as_trajan_app(db_session):
            result = await db_session.execute(
                text("SELECT id, email FROM users WHERE id = :id"), {"id": teammate.id}
            )
            row = result.one_or_none()

        assert row is not None, "teammate row was filtered — users_select_org_members missing?"
        assert row[0] == teammate.id
        assert row[1] is not None, "teammate email must be non-null (0.31.1 `?` placeholder bug)"

    @pytest.mark.asyncio
    async def test_user_does_not_read_stranger_row(
        self,
        db_session: AsyncSession,
        trajan_app_available: bool,
        as_trajan_app,
    ):
        """Negative case: a user with no shared org is invisible. Confirms
        the new policy did not accidentally become a directory-of-everyone."""
        if not trajan_app_available:
            pytest.skip("trajan_app role not present on this DB.")

        reader = await _make_user(db_session, label="reader")
        stranger = await _make_user(db_session, label="stranger")
        await organization_ops.create(db_session, name="[TEST] Reader Org", owner_id=reader.id)
        await organization_ops.create(db_session, name="[TEST] Stranger Org", owner_id=stranger.id)
        await db_session.flush()

        await set_rls_user_context(db_session, reader.id)
        async with as_trajan_app(db_session):
            result = await db_session.execute(
                text("SELECT id FROM users WHERE id = :id"), {"id": stranger.id}
            )
            assert result.scalar_one_or_none() is None

    @pytest.mark.asyncio
    async def test_shares_org_with_helper(
        self,
        db_session: AsyncSession,
        trajan_app_available: bool,
        as_trajan_app,
    ):
        """Direct unit-level check on the helper that powers the 0.31.1 policy.
        Catches a future SECURITY DEFINER → INVOKER refactor that would make
        the helper recurse through organization_members RLS."""
        if not trajan_app_available:
            pytest.skip("trajan_app role not present on this DB.")

        reader = await _make_user(db_session, label="reader")
        teammate = await _make_user(db_session, label="teammate")
        stranger = await _make_user(db_session, label="stranger")
        org = await organization_ops.create(
            db_session, name="[TEST] Helper Org", owner_id=reader.id
        )
        await _add_member(db_session, org_id=org.id, user_id=teammate.id, inviter_id=reader.id)
        await organization_ops.create(db_session, name="[TEST] Other Org", owner_id=stranger.id)
        await db_session.flush()

        await set_rls_user_context(db_session, reader.id)
        async with as_trajan_app(db_session):
            teammate_share = await db_session.execute(
                text("SELECT shares_org_with(:id)"), {"id": teammate.id}
            )
            stranger_share = await db_session.execute(
                text("SELECT shares_org_with(:id)"), {"id": stranger.id}
            )
            self_share = await db_session.execute(
                text("SELECT shares_org_with(:id)"), {"id": reader.id}
            )

        assert teammate_share.scalar_one() is True
        assert stranger_share.scalar_one() is False
        # A user shares org with themselves (owner membership exists) — this
        # is cosmetic because `users_select_own` already covers the self case,
        # but documenting the helper's behavior here prevents surprise.
        assert self_share.scalar_one() is True


# ─────────────────────────────────────────────────────────────────────────────
# TestWritePathsUnderRls
#
# The `users` INSERT fallback in app/api/deps/auth.py auto-provisions a User
# row when the Supabase signup trigger missed. Under `trajan_app` + FORCE RLS
# this path requires an INSERT policy that admits the authenticating user's
# own row — added by Phase 2.5 migration `9658836f9741_add_users_insert_self_policy`:
#
#     CREATE POLICY users_insert_self ON users
#         FOR INSERT WITH CHECK (id = app_user_id());
#
# These tests pin both sides of that policy: the positive path (self-insert
# admitted) and the negative path (insert for any other id is blocked).
# ─────────────────────────────────────────────────────────────────────────────


class TestWritePathsUnderRls:
    """Pinned state for RLS-sensitive write paths."""

    @pytest.mark.asyncio
    async def test_users_insert_self_policy_admits_own_row(
        self,
        db_session: AsyncSession,
        trajan_app_available: bool,
        as_trajan_app,
    ):
        """With `app.current_user_id` set to the new row's id, the
        `users_insert_self` policy admits the INSERT. This is the exact
        shape of the auth.py auto-provision fallback: `set_rls_user_context`
        runs with the JWT-derived id before the fallback INSERT.

        Before Phase 2.5 this raised "new row violates row-level security
        policy" under `trajan_app` and broke first-time login whenever the
        Supabase signup trigger missed.
        """
        if not trajan_app_available:
            pytest.skip("trajan_app role not present on this DB.")

        new_user_id = uuid.uuid4()
        await set_rls_user_context(db_session, new_user_id)

        async with as_trajan_app(db_session):
            await db_session.execute(
                text(
                    "INSERT INTO users (id, email, auth_provider) "
                    "VALUES (:id, :email, 'email')"
                ),
                {
                    "id": new_user_id,
                    "email": f"__test_insert_self_{new_user_id.hex[:8]}@example.com",
                },
            )
            await db_session.flush()

            # Read the row back under the same context — `users_select_own`
            # should see it. If RLS admitted the WITH CHECK but the row is
            # invisible to the author afterwards, something is inconsistent.
            result = await db_session.execute(
                text("SELECT id FROM users WHERE id = :id"), {"id": new_user_id}
            )
            assert result.scalar_one() == new_user_id

    @pytest.mark.asyncio
    async def test_users_insert_self_policy_blocks_other_rows(
        self,
        db_session: AsyncSession,
        trajan_app_available: bool,
        as_trajan_app,
    ):
        """The `WITH CHECK (id = app_user_id())` clause must reject an
        INSERT whose `id` does not match the current RLS context. Catches
        a regression to `WITH CHECK (true)` (or the policy being dropped
        entirely) — both of which would leave the positive case above
        still passing but let any authenticated user mint arbitrary
        users rows.

        Uses `begin_nested()` so the policy-violation error aborts only
        the SAVEPOINT, leaving the outer fixture transaction healthy
        for `RESET ROLE` on exit and for the next test's rollback.
        """
        if not trajan_app_available:
            pytest.skip("trajan_app role not present on this DB.")

        acting_user_id = uuid.uuid4()
        victim_user_id = uuid.uuid4()
        await set_rls_user_context(db_session, acting_user_id)

        async with as_trajan_app(db_session):
            with pytest.raises(DBAPIError) as excinfo:
                async with db_session.begin_nested():
                    await db_session.execute(
                        text(
                            "INSERT INTO users (id, email, auth_provider) "
                            "VALUES (:id, :email, 'email')"
                        ),
                        {
                            "id": victim_user_id,
                            "email": (
                                f"__test_insert_other_{victim_user_id.hex[:8]}@example.com"
                            ),
                        },
                    )

        # Confirm the failure mode is RLS policy (WITH CHECK), not a
        # GRANT issue, a UNIQUE collision, or an FK violation. Without
        # this assertion a future migration that removes the policy
        # but happens to leave the table ungranted would still pass.
        message = str(excinfo.value).lower()
        assert "row-level security" in message or "violates row-level" in message, (
            f"Expected RLS policy violation, got: {excinfo.value!r}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# TestBackgroundTaskRlsContext
#
# Plan B Phase 3 tripwire from
# docs/executing/known-user-background-task-rls-audit.md. Plan B Phases 1 & 2
# fixed 14 background-task sites that opened a fresh `async_session_maker()`
# session without re-establishing RLS context — under `trajan_app` + FORCE
# RLS, every RLS-protected query inside such a session silently returns zero
# rows. This test catches the file-level regression before it ships.
#
# The check is deliberately crude: presence of both strings in the same file,
# not AST-scoped to the same `async with` block. A file-level grep catches
# 100% of today's violations because every fresh-session block that needs
# context sets it exactly once per block, next to other blocks that already
# do the same. A strict AST-scoped upgrade is listed as a follow-up in the
# plan (§Open Questions #1).
# ─────────────────────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────────────────────
# TestRlsContextSurvivesCommit
#
# Plan B Phase 4 (post-commit RLS rehydration). Before B4, `SET LOCAL
# app.current_user_id` was dropped by every `session.commit()` and the next
# query ran under NULL context — silent zero-row reads and WITH CHECK
# violations under `trajan_app`. B4 added an `after_begin` listener on the
# sync `Session` class that re-issues `SET LOCAL` at the start of every new
# transaction if the session carries an `rls_user_id` on its `info` dict
# (populated by `set_rls_user_context`).
#
# The test below is the direct guard against the listener silently failing:
# open a session, set context, commit, then query and assert the context
# survived. If this goes red, the listener is not firing — the tactical
# re-calls at the 11 Phase-1 sites are keeping prod healthy but the
# structural fix is not in place.
# ─────────────────────────────────────────────────────────────────────────────


class TestRlsContextSurvivesCommit:
    """Phase 4: auto-rehydration listener re-arms SET LOCAL after commit."""

    @pytest.mark.asyncio
    async def test_rls_context_survives_commit(
        self,
        db_session: AsyncSession,
    ):
        """After `set_rls_user_context` + `commit()`, the next query on
        the same session must still see the user id. Pre-B4 this would
        return None (SET LOCAL dropped with the transaction). Post-B4
        the `after_begin` listener re-arms it from `session.info`."""
        user = await _make_user(db_session, label="commit-survive")
        await db_session.flush()

        await set_rls_user_context(db_session, user.id)
        # Force a real BEGIN/COMMIT cycle on the underlying connection.
        # `db_session` is the outer test-scoped session whose outer txn is
        # rolled back by the fixture — we use a nested transaction so the
        # inner commit exercises the listener without disturbing the outer
        # rollback.
        async with db_session.begin_nested():
            pass  # SAVEPOINT commit — `after_begin` fires when next query starts a tx

        current = await get_current_rls_user_id(db_session)
        assert current == user.id, (
            "RLS user context did not survive commit — the after_begin "
            "listener in app.core.database is not firing. Either the event "
            "is wired to the wrong Session class, session.info is not being "
            "populated by set_rls_user_context, or the SET LOCAL SQL is "
            "failing silently."
        )

    @pytest.mark.asyncio
    async def test_listener_noops_without_context(
        self,
        db_session: AsyncSession,
    ):
        """Sessions that never called `set_rls_user_context` (BYPASSRLS
        cron paths, tests that seed raw data) must not have context
        injected by the listener — otherwise a stray cron-session would
        pick up whatever user_id was lingering in thread-local state."""
        # Deliberately clear any context set by earlier tests in the suite.
        await clear_rls_context(db_session)

        async with db_session.begin_nested():
            pass

        current = await get_current_rls_user_id(db_session)
        assert current is None, (
            "Listener rehydrated context on a session with no rls_user_id "
            "in info — this would silently scope BYPASSRLS cron sessions "
            "to a stale user."
        )


class TestBackgroundTaskRlsContext:
    """Plan B Phase 3 tripwire — every consumer of `async_session_maker()`
    must also reference `set_rls_user_context` in the same file."""

    # Files under the companion cron-role plan
    # (docs/executing/cron-role-and-bypass-then-scope.md). Those paths will
    # gain bypass-then-scope discipline on a different schedule — some via
    # `cron_session_maker` (BYPASSRLS, no `set_rls_user_context` call at all),
    # some via a scoped `async_session_maker` session that does call it.
    # Pre-listed so this tripwire stays stable as cron-plan PRs land and so
    # the ownership boundary between the two plans is documented in code.
    _CRON_PLAN_OWNED = frozenset(
        {
            "services/scheduler.py",
            "api/v1/internal.py",
            "api/v1/webhooks.py",
            "api/v1/billing.py",
            "api/v1/public_tickets.py",
            "api/v1/mcp.py",
            "api/v1/partner.py",
            "api/v1/partner_config.py",
            "api/deps/api_key_auth.py",
            "api/deps/org_api_key_auth.py",
        }
    )

    # Infrastructure files that define the primitives themselves. `database.py`
    # holds `async_session_maker` + `get_db` (the unscoped wrapper that
    # `get_db_with_rls` decorates to add context); `rls.py` defines
    # `set_rls_user_context` itself.
    _INFRASTRUCTURE_FILES = frozenset({"database.py", "rls.py"})

    def test_async_session_maker_pairs_with_set_rls_user_context(self) -> None:
        """Every file in `backend/app` that calls `async_session_maker()`
        must also reference `set_rls_user_context` in the same file —
        otherwise the fresh session opens a new transaction with no
        `app.current_user_id`, and every RLS-protected query inside it
        silently returns zero rows under `trajan_app`.

        When this fails, the fix is nearly always one of:

          (a) Add `await set_rls_user_context(db, user_id)` as the first
              await inside the `async with async_session_maker() as db:`
              block. This is the Plan B pattern — 13 of the 14 known-user
              background-task sites were fixed this way.

          (b) If the site legitimately has no user identity at entry
              (cron, webhook, public-API key auth), move it under the
              cron-role plan in
              docs/executing/cron-role-and-bypass-then-scope.md and add
              its relative path to `_CRON_PLAN_OWNED`.
        """
        import pathlib

        # backend/tests/integration/test_rls_enforcement.py → backend/app
        app_root = pathlib.Path(__file__).resolve().parents[2] / "app"
        assert app_root.is_dir(), f"could not locate backend/app at {app_root}"

        offenders: list[str] = []
        for path in app_root.rglob("*.py"):
            relative = path.relative_to(app_root).as_posix()
            if relative in self._CRON_PLAN_OWNED:
                continue
            if path.name in self._INFRASTRUCTURE_FILES:
                continue

            src = path.read_text(encoding="utf-8")
            if "async_session_maker()" in src and "set_rls_user_context" not in src:
                offenders.append(relative)

        assert not offenders, (
            "Files open a fresh async_session_maker() without calling "
            "set_rls_user_context — under `trajan_app` + FORCE RLS every "
            "RLS-protected query inside the session will silently filter "
            "to zero rows. Add `await set_rls_user_context(db, user_id)` "
            "as the first await inside the `with` block, or — if the site "
            "genuinely has no user identity at entry — move it under the "
            "cron-role plan's allow-list (`_CRON_PLAN_OWNED`).\n"
            f"Offenders: {offenders}"
        )

    # Files permitted to call ``.commit()`` without referencing
    # ``set_rls_user_context`` or ``get_db_with_rls`` in the same file.
    #
    # The inclusion bar is: either the file legitimately runs under a
    # BYPASSRLS/API-key path (cron-plan territory) or its commits are
    # terminal-only with no subsequent DB work on the session.
    #
    # The test treats ``get_db_with_rls`` as an equivalent marker to
    # ``set_rls_user_context`` — a file that depends on the RLS-aware
    # session wrapper has proven it understands the invariant, and its
    # request-scoped commit is a one-shot that the outer ``get_db``
    # wrapper re-commits anyway.
    _COMMIT_ALLOWLIST = frozenset(
        {
            # ─── Cron-role plan territory (bypass-RLS / API-key auth) ───
            "services/scheduler.py",
            "api/v1/internal.py",
            "api/v1/webhooks.py",
            "api/v1/billing.py",
            "api/v1/public_tickets.py",
            "api/v1/mcp.py",
            "api/v1/partner.py",
            "api/v1/partner_config.py",
            "api/deps/api_key_auth.py",
            "api/deps/org_api_key_auth.py",
            # ─── Outside B4 scope (pre-existing gaps, not this plan's fix) ───
            # ``admin.py`` / ``github.py`` / ``progress/utils.py`` use plain
            # ``get_db`` (not ``get_db_with_rls``). Their RLS safety under
            # ``trajan_app`` is a separate audit that post-dates Plan A's
            # cron-role work. Flagging them here would drown B4's signal.
            "api/v1/admin.py",
            "api/v1/github.py",
            "api/v1/progress/utils.py",
            "api/v1/referrals.py",
            # ``auto_generator.py`` receives an already-contextualized
            # session from its caller (analysis orchestrator, which sets
            # context on a fresh session). Plan B Phase 2 fixed the caller;
            # this helper's commits are all under that outer context.
            "services/progress/auto_generator.py",
            # ``job_store.py`` is a shared helper called exclusively from
            # ``api/v1/documents/custom.py`` which already re-arms context
            # on every fresh session before calling these helpers.
            "services/docs/job_store.py",
        }
    )

    def test_commit_pairs_with_set_rls_user_context(self) -> None:
        """Every file in ``backend/app/services/**`` and
        ``backend/app/api/v1/**`` that calls ``.commit()`` on a session
        must also reference ``set_rls_user_context`` in the same file —
        otherwise a mid-flight commit drops ``SET LOCAL`` and the next
        query/refresh runs under NULL context, silently returning zero
        rows or raising WITH CHECK on inserts under ``trajan_app``.

        This is B4's Phase 3 extension to B3's file-level tripwire. It is
        deliberately coarser than AST-scoped analysis: the presence of
        ``set_rls_user_context`` anywhere in the file is enough to prove
        the author was aware of the invariant. That covers 100% of today's
        known patterns; a stricter upgrade (track each ``.commit()`` site
        back to its nearest enclosing async-with block) is listed as a
        follow-up in B4's plan §Open Questions #4.

        False positives (files whose only commit is a trivial terminal
        mutation with no further DB work) are listed in
        ``_COMMIT_ALLOWLIST``. The bar for allow-listing is high: prefer
        adding a dummy ``set_rls_user_context`` reference in a comment
        that names the invariant, since that future-proofs the file.
        """
        import pathlib
        import re

        app_root = pathlib.Path(__file__).resolve().parents[2] / "app"
        assert app_root.is_dir(), f"could not locate backend/app at {app_root}"

        # Target the two directories B4 is explicitly responsible for.
        scanned_roots = [app_root / "services", app_root / "api" / "v1"]

        # Match ``.commit()`` as a method call on any object (``db.commit()``,
        # ``self.db.commit()``, ``session.commit()``, etc.). Excludes the
        # literal string ``commit`` that might appear in comments or
        # docstrings without the trailing ``()``.
        commit_pattern = re.compile(r"\.commit\(\s*\)")
        # Either marker proves the file is RLS-context-aware:
        # - ``set_rls_user_context``: the file calls it itself (fresh
        #   sessions, or explicit re-arm after commit).
        # - ``get_db_with_rls``: the file's DB session comes from the
        #   RLS-wrapping dependency, so initial context is set and
        #   (post-B4) the after_begin listener re-arms across commits.
        rls_markers = ("set_rls_user_context", "get_db_with_rls")

        offenders: list[str] = []
        for root in scanned_roots:
            if not root.is_dir():
                continue
            for path in root.rglob("*.py"):
                relative = path.relative_to(app_root).as_posix()
                if relative in self._COMMIT_ALLOWLIST:
                    continue

                src = path.read_text(encoding="utf-8")
                if commit_pattern.search(src) and not any(m in src for m in rls_markers):
                    offenders.append(relative)

        assert not offenders, (
            "Files call `.commit()` on a session without also referencing "
            "`set_rls_user_context` — under `trajan_app` + FORCE RLS, "
            "`SET LOCAL app.current_user_id` is dropped at every commit, "
            "and the next query/refresh runs under NULL context (silent "
            "zero-row reads, WITH CHECK violations on INSERT). Fix by "
            "adding `await set_rls_user_context(session, user_id)` after "
            "the commit — or, if the commit is terminal with no further DB "
            "work on the session, add the file to `_COMMIT_ALLOWLIST` with "
            "a one-line justification.\n"
            f"Offenders: {offenders}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# TestPostCutoverWritePolicies
#
# Regression guards for the 11 policies added in migration
# `36e4127be7d3_rls_write_policies_post_cutover_sweep.py` (v0.31.9, plan at
# `docs/executing/rls-write-policy-sweep-post-trajan-app-cutover.md`).
#
# Each test seeds minimum preconditions under postgres (BYPASSRLS), sets the
# RLS user context, enters `as_trajan_app`, and exercises the write. A
# policy-drop or predicate-narrowing regression fails the happy-path test
# here; a predicate-widening regression is caught instead by Phase C's
# pg_policies coverage tripwire.
#
# These are direct DB-level tests — they don't exercise endpoint wiring or
# external service mocking. That's intentional: the gap closed in v0.31.9
# was purely a policy-expression gap, so the tightest regression guard is a
# direct INSERT/DELETE at the role boundary, not a full HTTP round-trip
# with GitHub and Claude mocks. Opt-in `rls_api_client` fixtures (see
# `tests/conftest.py`) are available for future HTTP-level coverage.
# ─────────────────────────────────────────────────────────────────────────────


async def _seed_user_org_product(
    db: AsyncSession, *, label: str
) -> tuple[User, uuid.UUID, uuid.UUID]:
    """Create a user who owns a fresh org containing a fresh product.

    All rows are inserted directly (not via `organization_ops.create`) to
    avoid the auto-subscription creation that would pre-populate a
    `subscriptions` row for tests that need to INSERT one themselves. The
    org owner is also added as an `organization_members` row with role=owner
    so `is_org_admin` / `can_edit_product` short-circuits work.
    """
    owner = await _make_user(db, label=label)

    org_id = uuid.uuid4()
    product_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO organizations (id, name, slug, owner_id) "
            "VALUES (:id, :name, :slug, :owner_id)"
        ),
        {
            "id": org_id,
            "name": f"Test Org ({label})",
            "slug": f"{label}-{org_id.hex[:8]}",
            "owner_id": owner.id,
        },
    )
    await db.execute(
        text(
            "INSERT INTO organization_members (organization_id, user_id, role) "
            "VALUES (:org_id, :user_id, 'owner')"
        ),
        {"org_id": org_id, "user_id": owner.id},
    )
    await db.execute(
        text(
            "INSERT INTO products (id, name, organization_id, user_id) "
            "VALUES (:id, :name, :org_id, :user_id)"
        ),
        {
            "id": product_id,
            "name": f"Test Product ({label})",
            "org_id": org_id,
            "user_id": owner.id,
        },
    )
    await db.flush()
    return owner, org_id, product_id


class TestPostCutoverWritePolicies:
    """Happy-path guards for the v0.31.9 RLS write-policy sweep."""

    @pytest.mark.asyncio
    async def test_commit_stats_cache_insert_admits_authenticated(
        self,
        db_session: AsyncSession,
        trajan_app_available: bool,
        as_trajan_app,
    ):
        """Any authenticated user can INSERT into the shared (repo_full_name, sha)
        cache. Originally 500d the Progress/Timeline tab on a freshly-imported
        repo hitting the cache-miss branch (e.g. `kaminocorp/cream`)."""
        if not trajan_app_available:
            pytest.skip("trajan_app role not present on this DB.")

        user = await _make_user(db_session, label="commit-cache")
        await set_rls_user_context(db_session, user.id)

        repo_full = f"test-org/test-repo-{uuid.uuid4().hex[:8]}"
        sha = uuid.uuid4().hex

        async with as_trajan_app(db_session):
            await db_session.execute(
                text(
                    "INSERT INTO commit_stats_cache "
                    "(repository_full_name, commit_sha, additions, deletions, files_changed) "
                    "VALUES (:repo, :sha, 10, 5, 2)"
                ),
                {"repo": repo_full, "sha": sha},
            )
            await db_session.flush()

            result = await db_session.execute(
                text(
                    "SELECT repository_full_name FROM commit_stats_cache "
                    "WHERE commit_sha = :sha"
                ),
                {"sha": sha},
            )
            assert result.scalar_one() == repo_full

    @pytest.mark.asyncio
    async def test_progress_summary_upsert_admits_editor(
        self,
        db_session: AsyncSession,
        trajan_app_available: bool,
        as_trajan_app,
    ):
        """`can_edit_product(product_id)` admits the org owner via the
        `has_product_access` short-circuit. Exercises both INSERT and UPDATE
        policies back-to-back since the production upsert path flows through
        both within one request."""
        if not trajan_app_available:
            pytest.skip("trajan_app role not present on this DB.")

        owner, _org_id, product_id = await _seed_user_org_product(
            db_session, label="progress-sum"
        )
        await set_rls_user_context(db_session, owner.id)

        async with as_trajan_app(db_session):
            await db_session.execute(
                text(
                    "INSERT INTO progress_summary "
                    "(product_id, period, summary_text, total_commits, total_contributors) "
                    "VALUES (:pid, '7d', 'first narrative', 42, 3)"
                ),
                {"pid": product_id},
            )
            await db_session.flush()

            await db_session.execute(
                text(
                    "UPDATE progress_summary SET summary_text = :t, total_commits = 50 "
                    "WHERE product_id = :pid AND period = '7d'"
                ),
                {"t": "second narrative", "pid": product_id},
            )
            await db_session.flush()

            result = await db_session.execute(
                text(
                    "SELECT summary_text FROM progress_summary "
                    "WHERE product_id = :pid AND period = '7d'"
                ),
                {"pid": product_id},
            )
            assert result.scalar_one() == "second narrative"

    @pytest.mark.asyncio
    async def test_dashboard_shipped_summary_insert_admits_editor(
        self,
        db_session: AsyncSession,
        trajan_app_available: bool,
        as_trajan_app,
    ):
        """Same shape as progress_summary — `can_edit_product` admits
        the org owner's INSERT."""
        if not trajan_app_available:
            pytest.skip("trajan_app role not present on this DB.")

        owner, _org_id, product_id = await _seed_user_org_product(
            db_session, label="dash-ship"
        )
        await set_rls_user_context(db_session, owner.id)

        async with as_trajan_app(db_session):
            await db_session.execute(
                text(
                    "INSERT INTO dashboard_shipped_summary "
                    "(product_id, period, items, has_significant_changes, total_commits) "
                    "VALUES (:pid, '7d', '[]'::jsonb, true, 12)"
                ),
                {"pid": product_id},
            )
            await db_session.flush()

            result = await db_session.execute(
                text(
                    "SELECT total_commits FROM dashboard_shipped_summary "
                    "WHERE product_id = :pid AND period = '7d'"
                ),
                {"pid": product_id},
            )
            assert result.scalar_one() == 12

    @pytest.mark.asyncio
    async def test_subscriptions_insert_admits_owner(
        self,
        db_session: AsyncSession,
        trajan_app_available: bool,
        as_trajan_app,
    ):
        """Predicate reads `organizations.owner_id` directly (not
        `is_org_admin`) to dodge the intra-flush-ordering race in
        `organization_ops.create()`. A user who owns the target org can
        INSERT the subscription row; a different user cannot."""
        if not trajan_app_available:
            pytest.skip("trajan_app role not present on this DB.")

        owner, org_id, _product_id = await _seed_user_org_product(
            db_session, label="sub-ins"
        )
        await set_rls_user_context(db_session, owner.id)

        async with as_trajan_app(db_session):
            await db_session.execute(
                text(
                    "INSERT INTO subscriptions "
                    "(organization_id, plan_tier, status, base_repo_limit, "
                    " cancel_at_period_end, referral_credit_cents, is_manually_assigned) "
                    "VALUES (:org_id, 'none', 'pending', 1, false, 0, false)"
                ),
                {"org_id": org_id},
            )
            await db_session.flush()

            result = await db_session.execute(
                text(
                    "SELECT plan_tier FROM subscriptions WHERE organization_id = :oid"
                ),
                {"oid": org_id},
            )
            assert result.scalar_one() == "none"

    @pytest.mark.asyncio
    async def test_subscriptions_delete_admits_owner(
        self,
        db_session: AsyncSession,
        trajan_app_available: bool,
        as_trajan_app,
    ):
        """The DELETE fires on the Stripe-webhook cascade path
        (`organizations → subscriptions`). The predicate's
        `organizations.owner_id = app_user_id()` check passes for the
        owner even when `organization_members` may already be cascading."""
        if not trajan_app_available:
            pytest.skip("trajan_app role not present on this DB.")

        owner, org_id, _product_id = await _seed_user_org_product(
            db_session, label="sub-del"
        )
        # Seed a subscription under postgres so the DELETE under trajan_app
        # is exercising only the DELETE policy, not both INSERT and DELETE.
        await db_session.execute(
            text(
                "INSERT INTO subscriptions "
                "(organization_id, plan_tier, status, base_repo_limit, "
                " cancel_at_period_end, referral_credit_cents, is_manually_assigned) "
                "VALUES (:org_id, 'indie', 'active', 10, false, 0, false)"
            ),
            {"org_id": org_id},
        )
        await db_session.flush()
        await set_rls_user_context(db_session, owner.id)

        async with as_trajan_app(db_session):
            await db_session.execute(
                text("DELETE FROM subscriptions WHERE organization_id = :oid"),
                {"oid": org_id},
            )
            await db_session.flush()

            result = await db_session.execute(
                text(
                    "SELECT count(*) FROM subscriptions WHERE organization_id = :oid"
                ),
                {"oid": org_id},
            )
            assert result.scalar_one() == 0

    @pytest.mark.asyncio
    async def test_product_api_keys_delete_admits_admin(
        self,
        db_session: AsyncSession,
        trajan_app_available: bool,
        as_trajan_app,
    ):
        """`can_admin_product(product_id)` admits the org owner's DELETE.
        Fires on `sa_delete(ProductApiKey).where(product_id == ...)`
        cascade inside `api/v1/products/crud.py` at product deletion."""
        if not trajan_app_available:
            pytest.skip("trajan_app role not present on this DB.")

        owner, _org_id, product_id = await _seed_user_org_product(
            db_session, label="pak-del"
        )
        # Seed an API key under postgres.
        key_id = uuid.uuid4()
        await db_session.execute(
            text(
                "INSERT INTO product_api_keys "
                "(id, product_id, key_hash, key_prefix, name, scopes, created_by_user_id) "
                "VALUES (:id, :pid, :hash, 'test_', 'Test Key', "
                " '[\"read\"]'::jsonb, :uid)"
            ),
            {
                "id": key_id,
                "pid": product_id,
                "hash": uuid.uuid4().hex,
                "uid": owner.id,
            },
        )
        await db_session.flush()
        await set_rls_user_context(db_session, owner.id)

        async with as_trajan_app(db_session):
            await db_session.execute(
                text("DELETE FROM product_api_keys WHERE product_id = :pid"),
                {"pid": product_id},
            )
            await db_session.flush()

            result = await db_session.execute(
                text(
                    "SELECT count(*) FROM product_api_keys WHERE product_id = :pid"
                ),
                {"pid": product_id},
            )
            assert result.scalar_one() == 0

    @pytest.mark.asyncio
    async def test_users_self_delete_admits_self(
        self,
        db_session: AsyncSession,
        trajan_app_available: bool,
        as_trajan_app,
    ):
        """`users_self_delete USING (id = app_user_id())` admits a user
        deleting their own row. Exercises the `user_ops.delete_with_data`
        account-deletion path (the Supabase auth-trigger cascade runs as
        `postgres` and bypasses RLS)."""
        if not trajan_app_available:
            pytest.skip("trajan_app role not present on this DB.")

        user = await _make_user(db_session, label="self-del")
        await set_rls_user_context(db_session, user.id)

        async with as_trajan_app(db_session):
            await db_session.execute(
                text("DELETE FROM users WHERE id = :uid"),
                {"uid": user.id},
            )
            await db_session.flush()

            # `users_select_own` would return this row pre-delete; after
            # delete the row is gone — and because `app_user_id()` still
            # points at the just-deleted id, every policy that reads
            # `organization_members` / owner_id now returns nothing for
            # this user. Simple existence check is enough to pin the fix.
            result = await db_session.execute(
                text("SELECT count(*) FROM users WHERE id = :uid"),
                {"uid": user.id},
            )
            assert result.scalar_one() == 0

    @pytest.mark.asyncio
    async def test_users_self_delete_rejects_other(
        self,
        db_session: AsyncSession,
        trajan_app_available: bool,
        as_trajan_app,
    ):
        """Negative guard — the DELETE predicate rejects deleting someone
        else. If a future refactor widens `USING (...)` to `USING (true)`,
        the positive test above still passes but this one fails."""
        if not trajan_app_available:
            pytest.skip("trajan_app role not present on this DB.")

        acting = await _make_user(db_session, label="acting")
        victim = await _make_user(db_session, label="victim")
        await set_rls_user_context(db_session, acting.id)

        async with as_trajan_app(db_session):
            await db_session.execute(
                text("DELETE FROM users WHERE id = :uid"),
                {"uid": victim.id},
            )
            await db_session.flush()

            # No exception — DELETE silently filters out rows the USING
            # predicate excludes. The victim row must still be present.
            result = await db_session.execute(
                text("SELECT count(*) FROM users WHERE id = :uid"),
                {"uid": victim.id},
            )
            assert result.scalar_one() == 1
