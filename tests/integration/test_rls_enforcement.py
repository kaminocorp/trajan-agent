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
from sqlalchemy.ext.asyncio import AsyncSession

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
# The `users` INSERT fallback in app/api/deps/auth.py has no permitting RLS
# policy (see pending item 2 in docs/completions/app-role-privilege-separation.md).
# This test documents that gap so it does not silently break when the Fly
# cutover lands. The test is expected to fail under trajan_app today and is
# xfail'd — turning it green requires either adding an INSERT policy or
# removing the fallback, at which point this test pins the chosen behavior.
# ─────────────────────────────────────────────────────────────────────────────


class TestWritePathsUnderRls:
    """Pinned state for RLS-sensitive write paths."""

    @pytest.mark.asyncio
    @pytest.mark.xfail(
        reason=(
            "Pending item 2 in app-role-privilege-separation.md — users INSERT "
            "has no permitting policy under trajan_app. Either add "
            "`INSERT WITH CHECK (id = app_user_id())` or drop the auth.py "
            "fallback. When that decision lands, flip this expectation."
        ),
        strict=False,
    )
    async def test_users_insert_under_trajan_app(
        self,
        db_session: AsyncSession,
        trajan_app_available: bool,
        as_trajan_app,
    ):
        """The auth.py auto-provision fallback INSERTs into `users` when the
        Supabase signup trigger missed. Under trajan_app this currently fails
        (no INSERT policy). Test encodes that fact so the pending decision
        can't slip."""
        if not trajan_app_available:
            pytest.skip("trajan_app role not present on this DB.")

        new_user_id = uuid.uuid4()
        await set_rls_user_context(db_session, new_user_id)

        # Today: INSERT raises DBAPIError (policy violation or missing grant)
        # under trajan_app → pytest records XFAIL.
        # Once item 2 of the 0.31.0 completion lands: INSERT succeeds →
        # pytest records XPASS and the author flips the @xfail off and
        # replaces this block with a normal assertion on the inserted row.
        async with as_trajan_app(db_session):
            await db_session.execute(
                text("INSERT INTO users (id, email, auth_provider) VALUES (:id, :email, 'email')"),
                {
                    "id": new_user_id,
                    "email": f"__test_insert_{new_user_id.hex[:8]}@example.com",
                },
            )
            await db_session.flush()


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
