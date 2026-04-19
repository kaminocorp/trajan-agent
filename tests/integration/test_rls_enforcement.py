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

from app.core.rls import clear_rls_context, set_rls_user_context
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
