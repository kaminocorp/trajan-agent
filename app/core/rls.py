"""Row-Level Security (RLS) context management.

This module provides functions to set and manage the PostgreSQL session context
used by RLS policies to determine the current authenticated user.

Key concepts:
- Uses SET LOCAL for transaction-scoped settings (works with PgBouncer pooling)
- The app.current_user_id setting is read by RLS policies via app_user_id() function
- Service role bypasses RLS automatically (configured in migration)

Usage:
    # In request handler or dependency
    await set_rls_user_context(session, user.id)
    # All subsequent queries in this transaction will be filtered by RLS
"""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# Key stored on ``session.info`` so the ``after_begin`` listener (see
# :mod:`app.core.database`) can re-issue ``SET LOCAL`` at the start of every
# new transaction on the session. This is what makes the invariant survive
# mid-flight ``commit()``s without each call site having to re-call
# ``set_rls_user_context`` manually.
RLS_INFO_KEY = "rls_user_id"


async def set_rls_user_context(session: AsyncSession, user_id: UUID) -> None:
    """
    Set the current user context for RLS policies.

    Uses SET LOCAL to ensure the setting is transaction-scoped,
    which works correctly with connection poolers like PgBouncer.
    The setting is automatically reset when the transaction ends —
    the ``after_begin`` listener on :class:`sqlalchemy.orm.Session`
    re-issues it for every subsequent transaction on this session.

    Args:
        session: The async database session
        user_id: The authenticated user's UUID

    Example:
        async with async_session_maker() as session:
            await set_rls_user_context(session, current_user.id)
            # All queries now filtered by RLS policies — even across
            # mid-flight commits, because the listener re-arms context
            # at the start of every new transaction.
            products = await session.execute(select(Product))
    """
    # Persist on sync_session.info so the auto-rehydration listener can
    # retrieve it. The listener only attaches to the sync Session, so we
    # write to the sync_session's info dict (it is shared with AsyncSession).
    session.sync_session.info[RLS_INFO_KEY] = user_id

    # SET LOCAL doesn't support parameterized queries ($1 placeholders) in PostgreSQL.
    # This is safe because user_id is a validated UUID type (only hex chars and hyphens).
    await session.execute(text(f"SET LOCAL app.current_user_id = '{user_id}'"))


async def clear_rls_context(session: AsyncSession) -> None:
    """
    Clear the RLS user context.

    This is optional since SET LOCAL automatically resets at transaction end,
    but can be useful for explicit cleanup in tests or when reusing sessions.

    Also removes the stashed user_id from ``session.info`` so the
    ``after_begin`` listener stops re-arming context on this session.

    Args:
        session: The async database session
    """
    session.sync_session.info.pop(RLS_INFO_KEY, None)
    await session.execute(text("RESET app.current_user_id"))


async def get_current_rls_user_id(session: AsyncSession) -> UUID | None:
    """
    Get the currently set RLS user ID from the session.

    Useful for debugging and testing to verify RLS context is correctly set.

    Args:
        session: The async database session

    Returns:
        The current user UUID if set, None otherwise
    """
    result = await session.execute(
        text("SELECT NULLIF(current_setting('app.current_user_id', true), '')::uuid")
    )
    row = result.scalar_one_or_none()
    return row


# SQL expression for use in RLS policies (defined in migration)
# Usage in policies: current_setting('app.current_user_id', true)::uuid
# The app_user_id() helper function wraps this for convenience

RLS_USER_ID_SQL = "current_setting('app.current_user_id', true)::uuid"
