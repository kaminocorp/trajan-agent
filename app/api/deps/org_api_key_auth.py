"""Organisation API key authentication dependencies for partner endpoints.

Validates API keys from Authorization: Bearer trj_org_xxx headers
and enforces scope-based access control.
"""

from collections.abc import Awaitable, Callable

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.domain.org_api_key_operations import OrgApiKeyOperations, org_api_key_ops
from app.models.org_api_key import OrgApiKey

org_api_key_security = HTTPBearer(auto_error=False)


async def get_org_api_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(org_api_key_security),
    db: AsyncSession = Depends(get_db),
) -> OrgApiKey:
    """Validate org-scoped API key from Bearer token and return the key record.

    Uses plain get_db (no RLS) — partner endpoints scope queries
    by api_key.organization_id.
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key required",
        )

    api_key = await org_api_key_ops.validate_key(db, credentials.credentials)
    if api_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked API key",
        )

    return api_key


def require_partner_scope(scope: str) -> Callable[..., Awaitable[OrgApiKey]]:
    """Return a dependency that validates an org API key has the required scope."""

    async def _check_scope(
        api_key: OrgApiKey = Depends(get_org_api_key),
    ) -> OrgApiKey:
        if not OrgApiKeyOperations.check_scope(api_key, scope):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"API key missing required scope: {scope}",
            )
        return api_key

    return _check_scope
