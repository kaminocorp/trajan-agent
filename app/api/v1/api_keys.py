"""API key management endpoints (authenticated, product-scoped)."""

import uuid as uuid_pkg

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps.auth import get_current_user, get_db_with_rls
from app.api.deps.feature_gates import require_product_subscription
from app.api.deps.product_access import (
    check_product_admin_access,
    check_product_editor_access,
)
from app.domain.product_api_key_operations import api_key_ops
from app.models.product_api_key import (
    ProductApiKeyCreate,
    ProductApiKeyCreateResponse,
    ProductApiKeyRead,
)
from app.models.user import User

router = APIRouter(
    prefix="/products/{product_id}/api-keys",
    tags=["api-keys"],
)

ALLOWED_SCOPES = {"tickets:write", "tickets:read", "mcp:read", "mcp:write", "mcp:admin"}


async def _require_api_access(db: AsyncSession, product_id: uuid_pkg.UUID) -> None:
    """Check that the product's org plan includes API access."""
    ctx = await require_product_subscription(db, product_id)
    if not ctx.plan.api_access:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="API access requires a Pro or Scale plan.",
        )


@router.get("", response_model=list[ProductApiKeyRead])
async def list_api_keys(
    product_id: uuid_pkg.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> list[ProductApiKeyRead]:
    """List active API keys for a product (editor+ access)."""
    await check_product_editor_access(db, product_id, current_user.id)
    await _require_api_access(db, product_id)
    keys = await api_key_ops.list_by_product(db, product_id)
    return [ProductApiKeyRead.model_validate(k) for k in keys]


@router.post(
    "",
    response_model=ProductApiKeyCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_api_key(
    product_id: uuid_pkg.UUID,
    data: ProductApiKeyCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> ProductApiKeyCreateResponse:
    """Create a new API key (admin access). The raw key is shown once."""
    await check_product_admin_access(db, product_id, current_user.id)
    await _require_api_access(db, product_id)
    invalid_scopes = set(data.scopes) - ALLOWED_SCOPES
    if invalid_scopes:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid scopes: {', '.join(sorted(invalid_scopes))}. "
            f"Allowed: {', '.join(sorted(ALLOWED_SCOPES))}",
        )
    api_key, raw_key = await api_key_ops.create_key(
        db,
        product_id=product_id,
        name=data.name,
        scopes=data.scopes,
        created_by_user_id=current_user.id,
    )
    return ProductApiKeyCreateResponse(
        **ProductApiKeyRead.model_validate(api_key).model_dump(),
        raw_key=raw_key,
    )


@router.delete("/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(
    product_id: uuid_pkg.UUID,
    key_id: uuid_pkg.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> None:
    """Revoke an API key (admin access, soft-delete)."""
    await check_product_admin_access(db, product_id, current_user.id)
    key = await api_key_ops.get(db, key_id)
    if not key or key.product_id != product_id or key.revoked_at is not None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="API key not found.",
        )
    await api_key_ops.revoke(db, key)
