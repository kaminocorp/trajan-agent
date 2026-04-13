"""Organisation API key management endpoints (authenticated, org-scoped).

Allows org admins to create, list, and revoke organisation-level API keys
used by partner integrations (e.g. intranet dashboards).
"""

import uuid as uuid_pkg

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps.auth import get_current_user, get_db_with_rls
from app.api.deps.organization import require_org_admin
from app.domain.org_api_key_operations import org_api_key_ops
from app.models.org_api_key import (
    ALLOWED_ORG_KEY_SCOPES,
    OrgApiKeyCreate,
    OrgApiKeyCreateResponse,
    OrgApiKeyRead,
)
from app.models.organization import Organization
from app.models.user import User

router = APIRouter(
    prefix="/organizations/{org_id}/api-keys",
    tags=["org-api-keys"],
)


@router.get("", response_model=list[OrgApiKeyRead])
async def list_org_api_keys(
    org_id: uuid_pkg.UUID,  # noqa: ARG001 — path param consumed by FastAPI
    _current_user: User = Depends(get_current_user),
    org: Organization = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db_with_rls),
) -> list[OrgApiKeyRead]:
    """List active API keys for the organisation (admin+ access)."""
    keys = await org_api_key_ops.list_by_org(db, org.id)
    return [OrgApiKeyRead.model_validate(k) for k in keys]


@router.post(
    "",
    response_model=OrgApiKeyCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_org_api_key(
    org_id: uuid_pkg.UUID,  # noqa: ARG001 — path param consumed by FastAPI
    data: OrgApiKeyCreate,
    current_user: User = Depends(get_current_user),
    org: Organization = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db_with_rls),
) -> OrgApiKeyCreateResponse:
    """Create a new organisation API key (admin+ access). The raw key is shown once."""
    invalid_scopes = set(data.scopes) - ALLOWED_ORG_KEY_SCOPES
    if invalid_scopes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid scopes: {', '.join(sorted(invalid_scopes))}. "
            f"Allowed: {', '.join(sorted(ALLOWED_ORG_KEY_SCOPES))}",
        )
    api_key, raw_key = await org_api_key_ops.create_key(
        db,
        organization_id=org.id,
        name=data.name,
        scopes=data.scopes,
        created_by_user_id=current_user.id,
    )
    return OrgApiKeyCreateResponse(
        **OrgApiKeyRead.model_validate(api_key).model_dump(),
        raw_key=raw_key,
    )


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_org_api_key(
    org_id: uuid_pkg.UUID,  # noqa: ARG001 — path param consumed by FastAPI
    key_id: uuid_pkg.UUID,
    _current_user: User = Depends(get_current_user),
    org: Organization = Depends(require_org_admin),
    db: AsyncSession = Depends(get_db_with_rls),
) -> None:
    """Revoke an organisation API key (admin+ access, soft-delete)."""
    key = await org_api_key_ops.get(db, key_id)
    if not key or key.organization_id != org.id or key.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="API key not found.",
        )
    await org_api_key_ops.revoke(db, key)
