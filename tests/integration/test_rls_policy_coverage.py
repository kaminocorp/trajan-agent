"""Phase C CI tripwire for RLS policy coverage.

Each RLS-enabled public table must have a `pg_policies.cmd` set that is a
**superset** of its entry in `app.core.rls_policy_allowlist`. If a future
migration drops or narrows a policy (or enables RLS on a new table without
adding write policies), this test fails before the change ships.

The corresponding class of bug — user-request writes silently rejected by
RLS under `trajan_app` — drove every patch from v0.31.1 through v0.31.9.
The allowlist encodes the *state v0.31.9 achieved*; drift from there now
shows up as a test failure rather than a production 500.

Three orthogonal checks:

- `test_every_rls_table_in_allowlist` — every RLS-enabled public table has
  an allowlist entry. Catches `ALTER TABLE x ENABLE ROW LEVEL SECURITY`
  landing without a conscious allowlist decision.
- `test_every_allowlist_table_is_rls_enabled` — allowlist entries
  correspond to actual RLS-enabled tables. Keeps the allowlist honest
  when tables are removed.
- `test_policy_cmds_cover_allowlist` — the actual verbs covered by
  `pg_policies` for each table are a superset of the allowlist. This is
  the direct regression guard for the v0.31.9 fix class.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rls_policy_allowlist import ALL_VERBS, RLS_POLICY_ALLOWLIST


@pytest.mark.asyncio
async def test_every_rls_table_in_allowlist(db_session: AsyncSession) -> None:
    """Every RLS-enabled public table must have a `RLS_POLICY_ALLOWLIST`
    entry. Forces a conscious decision when a new RLS-enabled table
    ships — "oops, forgot to add policies" fails CI loudly."""
    result = await db_session.execute(
        text(
            "SELECT relname FROM pg_class "
            "WHERE relnamespace = 'public'::regnamespace "
            "  AND relkind = 'r' "
            "  AND relrowsecurity = true "
            "ORDER BY relname"
        )
    )
    rls_tables = {row[0] for row in result.all()}
    missing = rls_tables - RLS_POLICY_ALLOWLIST.keys()
    assert not missing, (
        f"RLS-enabled tables without an allowlist entry: {sorted(missing)}.\n"
        "Add each to `backend/app/core/rls_policy_allowlist.py` with the "
        "expected policy-cmd set and a one-line justification. If the table "
        "has no app-code writers (service-role-only), use `frozenset({'SELECT'})` "
        "and document the exception."
    )


@pytest.mark.asyncio
async def test_every_allowlist_table_is_rls_enabled(db_session: AsyncSession) -> None:
    """`RLS_POLICY_ALLOWLIST` entries must correspond to tables that exist
    and have RLS enabled. Prevents the allowlist from accumulating stale
    entries that silently weaken coverage."""
    result = await db_session.execute(
        text(
            "SELECT relname FROM pg_class "
            "WHERE relnamespace = 'public'::regnamespace "
            "  AND relkind = 'r' "
            "  AND relrowsecurity = true"
        )
    )
    rls_tables = {row[0] for row in result.all()}
    stale = set(RLS_POLICY_ALLOWLIST.keys()) - rls_tables
    assert not stale, (
        f"Allowlist entries for tables that don't exist or have RLS disabled: "
        f"{sorted(stale)}.\n"
        "Remove them from `backend/app/core/rls_policy_allowlist.py`."
    )


@pytest.mark.asyncio
async def test_policy_cmds_cover_allowlist(db_session: AsyncSession) -> None:
    """`pg_policies` for each RLS-enabled table must cover at least the
    verbs listed in the allowlist. `ALL` is expanded to all four verbs.

    Gaps indicate either (a) a policy was dropped and the allowlist was
    not updated, or (b) a new verb was added to the allowlist without a
    corresponding migration. Either way the fix is explicit — ship a
    policy migration or narrow the allowlist with documented rationale.
    """
    result = await db_session.execute(
        text(
            "SELECT tablename, array_agg(DISTINCT cmd) AS cmds "
            "FROM pg_policies "
            "WHERE schemaname = 'public' "
            "GROUP BY tablename"
        )
    )
    coverage_by_table: dict[str, set[str]] = {}
    for row in result.all():
        cmds = set(row.cmds)
        if "ALL" in cmds:
            cmds = cmds | ALL_VERBS
        coverage_by_table[row.tablename] = cmds

    gaps: list[str] = []
    for table, required in RLS_POLICY_ALLOWLIST.items():
        actual = coverage_by_table.get(table, set())
        missing = required - actual
        if missing:
            gaps.append(
                f"  {table}: need {sorted(required)}, "
                f"have {sorted(actual) or '[]'}, "
                f"missing {sorted(missing)}"
            )

    assert not gaps, (
        "RLS policy coverage is narrower than the allowlist requires.\n"
        "This is the v0.31.9 class of bug: a user-request code path will "
        "fail with `InsufficientPrivilegeError` (surfaces as 500) for every "
        "write the missing verb would have permitted. Either:\n"
        "  (a) add the missing policy in a new alembic migration, OR\n"
        "  (b) narrow the allowlist entry in "
        "`backend/app/core/rls_policy_allowlist.py` with documented "
        "rationale (e.g. the table's write contract really did shrink).\n\n"
        "Gaps:\n" + "\n".join(gaps)
    )
