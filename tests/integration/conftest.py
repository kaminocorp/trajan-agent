"""Integration test conftest — DB rollback fixtures.

Inherits the root conftest.py fixtures (db_session, test_user, etc.)
and adds integration-specific markers plus the `trajan_app` role-
switching helpers used by RLS-enforcement tests.

All tests in this directory use the transaction-rollback pattern:
real SQL executes, but nothing persists.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture(autouse=True)
def _mark_integration(request):
    """Auto-mark all tests in this directory as integration."""
    request.node.add_marker(pytest.mark.integration)


# ─────────────────────────────────────────────────────────────────────────────
# trajan_app role helpers
#
# The db_session fixture connects as `postgres` (DATABASE_URL_DIRECT). That
# role has rolbypassrls=true on Supabase, so RLS is silently bypassed for
# every query seeded through it. To actually exercise the policies we need
# to run queries under a role with rolbypassrls=false — `trajan_app`.
#
# Approach: seed data as `postgres` (bypass), then `SET LOCAL ROLE trajan_app`
# and `SET LOCAL app.current_user_id = …` before the assertions. `SET LOCAL`
# is transaction-scoped, so everything resets when the outer rollback fires.
#
# trajan_app is a per-environment DBA operation (see
# docs/completions/app-role-privilege-separation.md, section 2). If the role
# doesn't exist on the test DB, the role-scoped tests skip with a clear
# reason rather than failing.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
async def trajan_app_available(db_session: AsyncSession) -> bool:
    """Return True iff the `trajan_app` role exists on the test DB.

    Tests that must run under the non-bypass role call this first and skip
    otherwise — local dev DBs that haven't had the DBA role-creation step
    applied (see completion doc 0.31.0) would otherwise fail noisily.
    """
    result = await db_session.execute(text("SELECT 1 FROM pg_roles WHERE rolname = 'trajan_app'"))
    return result.scalar_one_or_none() is not None


@asynccontextmanager
async def _as_trajan_app(session: AsyncSession) -> AsyncIterator[None]:
    """Run the enclosed block with `SET LOCAL ROLE trajan_app` active.

    Resets role on exit. The `SET LOCAL` is transaction-scoped so the outer
    rollback in db_session would also revert it, but RESET ROLE keeps the
    session clean if the caller continues seeding data afterwards.
    """
    try:
        await session.execute(text("SET LOCAL ROLE trajan_app"))
    except (ProgrammingError, DBAPIError) as exc:
        pytest.skip(f"Cannot SET LOCAL ROLE trajan_app ({exc}); role missing or ungranted.")
    try:
        yield
    finally:
        await session.execute(text("RESET ROLE"))


@pytest.fixture
def as_trajan_app():
    """Fixture form of `_as_trajan_app`: returns the context manager itself.

    Usage:
        async with as_trajan_app(db_session):
            # queries run under trajan_app (NOBYPASSRLS + FORCE RLS evaluated)
    """
    return _as_trajan_app
