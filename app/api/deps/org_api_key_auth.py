"""Organisation API key authentication dependencies for partner endpoints.

Validates API keys from ``Authorization: Bearer trj_org_xxx`` headers
and enforces scope-based access control.

Bypass-then-scope (Phase 3.0b of cron-role plan):

``org_api_keys`` is RLS-protected with an admin-scoped SELECT policy that
requires ``app_user_id()``. Under ``trajan_app`` at request entry there is
no user context yet — validating the key on the pooled app engine would
return zero rows and 401 every partner request.

We validate on ``cron_session_maker`` (BYPASSRLS, narrow lookup) and
resolve an ``effective_user_id`` for downstream RLS context:

- ``api_key.created_by_user_id`` if non-null (preferred — tied to a real
  org member who minted the key),
- otherwise fall back to ``organizations.owner_id`` (NOT NULL) so a key
  whose creator was later deleted still works.

The fallback resolves in the same bootstrap session, so handlers receive
a fully-resolved ``PartnerAuthContext`` and don't need their own lookup.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import text

from app.core.database import cron_session_maker
from app.domain.org_api_key_operations import OrgApiKeyOperations, org_api_key_ops
from app.models.org_api_key import OrgApiKey

org_api_key_security = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class PartnerAuthContext:
    """Partner-auth context passed to every handler that depends on an org API key.

    ``effective_user_id`` is what handlers must pass to
    :func:`set_rls_user_context` — it is ``api_key.created_by_user_id`` when
    non-null, otherwise the owning ``organizations.owner_id``. The fallback
    keeps keys created by users who have since been deleted from working
    without widening the SELECT policy on ``org_api_keys``.
    """

    api_key: OrgApiKey
    effective_user_id: UUID


async def get_org_api_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(org_api_key_security),
) -> PartnerAuthContext:
    """Validate org-scoped API key and resolve RLS context.

    Runs on ``cron_session_maker`` (BYPASSRLS). Returns a frozen
    :class:`PartnerAuthContext` so handlers never construct their own
    bootstrap — ensures nobody accidentally widens the bypass surface.
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key required",
        )

    async with cron_session_maker() as cron_db:
        api_key = await org_api_key_ops.validate_key(cron_db, credentials.credentials)
        if api_key is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or revoked API key",
            )

        effective_user_id = api_key.created_by_user_id
        if effective_user_id is None:
            # Fallback: the key's creator was deleted (ON DELETE SET NULL on
            # ``created_by_user_id``). Resolve the org's canonical owner.
            owner_result = await cron_db.execute(
                text("SELECT owner_id FROM organizations WHERE id = :oid"),
                {"oid": str(api_key.organization_id)},
            )
            effective_user_id = owner_result.scalar_one_or_none()
            if effective_user_id is None:
                # FK design should make this impossible (org deletion ought to
                # cascade to org_api_keys), but treating "impossible" as a 500
                # leaks a stack trace on a clean auth-failure shape. Fail as
                # 401 so the caller sees a deterministic auth error.
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="API key organization no longer exists",
                )

    return PartnerAuthContext(api_key=api_key, effective_user_id=effective_user_id)


def require_partner_scope(scope: str) -> Callable[..., Awaitable[PartnerAuthContext]]:
    """Return a dependency that validates an org API key has the required scope."""

    async def _check_scope(
        ctx: PartnerAuthContext = Depends(get_org_api_key),
    ) -> PartnerAuthContext:
        if not OrgApiKeyOperations.check_scope(ctx.api_key, scope):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"API key missing required scope: {scope}",
            )
        return ctx

    return _check_scope
