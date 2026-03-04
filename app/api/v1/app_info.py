import uuid as uuid_pkg

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    get_current_user,
    get_db_with_rls,
    require_product_subscription,
)
from app.core.rate_limit import EXPORT_LIMIT, REVEAL_LIMIT, rate_limiter
from app.domain import app_info_ops
from app.domain.app_info_operations import validate_tags
from app.domain.organization_operations import organization_ops
from app.domain.product_access_operations import product_access_ops
from app.domain.product_operations import product_ops
from app.models.app_info import (
    AppInfoBulkCreate,
    AppInfoBulkResponse,
    AppInfoCreate,
    AppInfoExportEntry,
    AppInfoExportResponse,
    AppInfoTagsResponse,
    AppInfoUpdate,
)
from app.models.organization import MemberRole
from app.models.user import User

router = APIRouter(prefix="/app-info", tags=["app info"])

# Bulk import limits
MAX_BULK_ENTRIES = 500  # Maximum entries in a single bulk import
MAX_KEY_LENGTH = 255  # Maximum length of a key
MAX_VALUE_LENGTH = 10000  # Maximum length of a value (10KB)


async def _check_variables_access(
    db: AsyncSession,
    product_id: uuid_pkg.UUID,
    user_id: uuid_pkg.UUID,
) -> None:
    """
    Check if user has access to environment variables for a product.

    Raises 404 if product not found.
    Raises 403 if user doesn't have editor/admin access.
    """
    # Get product to find its organization
    product = await product_ops.get(db, product_id)
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found",
        )

    # Get user's org role
    if not product.organization_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Product is not associated with an organization",
        )
    org_role = await organization_ops.get_member_role(db, product.organization_id, user_id)
    if not org_role:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not a member of this organization",
        )

    # Check if user can access variables (editor or admin only)
    can_access = await product_access_ops.user_can_access_variables(
        db, product_id, user_id, org_role
    )
    if not can_access:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You don't have access to environment variables. "
            "Please contact your project Admin to request Editor access.",
        )


@router.get("", response_model=list[dict])
async def list_app_info(
    product_id: uuid_pkg.UUID = Query(..., description="Filter by product"),
    tags: str | None = Query(
        None, description="Filter by tags (comma-separated). Returns entries with ALL tags."
    ),
    skip: int = 0,
    limit: int = 100,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """List app info entries for a product with optional tag filtering."""
    # Check variables access
    await _check_variables_access(db, product_id, current_user.id)

    # Parse tags from comma-separated string
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else None

    entries = await app_info_ops.get_by_product_for_org(
        db,
        product_id=product_id,
        tags=tag_list,
        skip=skip,
        limit=limit,
    )
    return [
        {
            "id": str(e.id),
            "key": e.key,
            "value": "********" if e.is_secret else e.value,
            "category": e.category,
            "is_secret": e.is_secret,
            "description": e.description,
            "target_file": e.target_file,
            "tags": e.tags or [],
            "product_id": str(e.product_id) if e.product_id else None,
            "created_at": e.created_at.isoformat(),
            "updated_at": e.updated_at.isoformat(),
        }
        for e in entries
    ]


@router.get("/tags", response_model=AppInfoTagsResponse)
async def get_product_tags(
    product_id: uuid_pkg.UUID = Query(..., description="Product to get tags for"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """Get all unique tags used across app info entries for a product.

    Returns a sorted list of unique tags for use in autocomplete/suggestions.
    """
    # Check variables access
    await _check_variables_access(db, product_id, current_user.id)

    tags = await app_info_ops.get_all_tags_for_org(
        db,
        product_id=product_id,
    )
    return AppInfoTagsResponse(tags=tags)


@router.get("/export", response_model=AppInfoExportResponse)
async def export_app_info(
    product_id: uuid_pkg.UUID = Query(..., description="Product to export from"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """
    Export all app info entries with revealed secret values.

    Returns all entries for a product with their actual values (secrets unmasked),
    ready for formatting as a .env file.

    **Authorization:** Requires org admin or owner role (not just editor access).
    **Rate limited:** 10 requests per minute per user.
    """
    # Check variables access (editor or above)
    await _check_variables_access(db, product_id, current_user.id)

    # Additional authorization: export requires admin/owner role
    product = await product_ops.get(db, product_id)
    if not product or not product.organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found",
        )

    org_role = await organization_ops.get_member_role(db, product.organization_id, current_user.id)
    if org_role not in (MemberRole.OWNER.value, MemberRole.ADMIN.value):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Export requires organization admin or owner access",
        )

    # Rate limiting
    rate_limiter.check_rate_limit(current_user.id, "app_info_export", EXPORT_LIMIT)

    entries = await app_info_ops.get_by_product_for_org(
        db,
        product_id=product_id,
        limit=1000,  # Reasonable limit for export
    )

    return AppInfoExportResponse(
        entries=[
            AppInfoExportEntry(
                key=e.key or "",
                # Decrypt the value for export
                value=app_info_ops.decrypt_entry_value(e) or "",
                category=e.category,
                is_secret=e.is_secret or False,
                description=e.description,
                target_file=e.target_file,
                tags=e.tags or [],
            )
            for e in entries
        ]
    )


@router.get("/{app_info_id}")
async def get_app_info(
    app_info_id: uuid_pkg.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """Get a single app info entry."""
    entry = await app_info_ops.get(db, id=app_info_id)
    if not entry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="App info not found",
        )

    # Check variables access
    if entry.product_id:
        await _check_variables_access(db, entry.product_id, current_user.id)

    return {
        "id": str(entry.id),
        "key": entry.key,
        "value": "********" if entry.is_secret else entry.value,
        "category": entry.category,
        "is_secret": entry.is_secret,
        "description": entry.description,
        "target_file": entry.target_file,
        "tags": entry.tags or [],
        "product_id": str(entry.product_id) if entry.product_id else None,
        "created_at": entry.created_at.isoformat(),
        "updated_at": entry.updated_at.isoformat(),
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_app_info(
    data: AppInfoCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """Create a new app info entry."""
    await _check_variables_access(db, data.product_id, current_user.id)
    await require_product_subscription(db, data.product_id)

    # Validate tags
    tag_errors = validate_tags(data.tags)
    if tag_errors:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid tags: {'; '.join(tag_errors)}",
        )

    # Check for duplicate key in product
    existing = await app_info_ops.get_by_key(
        db, user_id=current_user.id, product_id=data.product_id, key=data.key
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="App info with this key already exists for this product",
        )

    entry = await app_info_ops.create(
        db,
        obj_in=data.model_dump(),
        user_id=current_user.id,
    )
    return {
        "id": str(entry.id),
        "key": entry.key,
        "value": "********" if entry.is_secret else entry.value,
        "category": entry.category,
        "is_secret": entry.is_secret,
        "description": entry.description,
        "target_file": entry.target_file,
        "tags": entry.tags or [],
        "product_id": str(entry.product_id) if entry.product_id else None,
        "created_at": entry.created_at.isoformat(),
        "updated_at": entry.updated_at.isoformat(),
    }


@router.patch("/{app_info_id}")
async def update_app_info(
    app_info_id: uuid_pkg.UUID,
    data: AppInfoUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """Update an app info entry."""
    entry = await app_info_ops.get(db, id=app_info_id)
    if not entry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="App info not found",
        )

    # Check variables access
    if entry.product_id:
        await _check_variables_access(db, entry.product_id, current_user.id)
        await require_product_subscription(db, entry.product_id)

    # Validate tags if provided
    if data.tags is not None:
        tag_errors = validate_tags(data.tags)
        if tag_errors:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid tags: {'; '.join(tag_errors)}",
            )

    updated = await app_info_ops.update(
        db, db_obj=entry, obj_in=data.model_dump(exclude_unset=True)
    )
    return {
        "id": str(updated.id),
        "key": updated.key,
        "value": "********" if updated.is_secret else updated.value,
        "category": updated.category,
        "is_secret": updated.is_secret,
        "description": updated.description,
        "target_file": updated.target_file,
        "tags": updated.tags or [],
        "product_id": str(updated.product_id) if updated.product_id else None,
        "created_at": updated.created_at.isoformat(),
        "updated_at": updated.updated_at.isoformat(),
    }


@router.delete("/{app_info_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_app_info(
    app_info_id: uuid_pkg.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """Delete an app info entry."""
    # First get the entry to check access
    entry = await app_info_ops.get(db, id=app_info_id)
    if not entry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="App info not found",
        )

    # Check variables access
    if entry.product_id:
        await _check_variables_access(db, entry.product_id, current_user.id)
        await require_product_subscription(db, entry.product_id)

    await db.delete(entry)
    await db.flush()


@router.get("/{app_info_id}/reveal")
async def reveal_app_info_value(
    app_info_id: uuid_pkg.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """Reveal the actual value of a secret app info entry for copying.

    **Rate limited:** 30 requests per minute per user.
    """
    entry = await app_info_ops.get(db, id=app_info_id)
    if not entry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="App info not found",
        )

    # Check variables access
    if entry.product_id:
        await _check_variables_access(db, entry.product_id, current_user.id)

    # Rate limiting
    rate_limiter.check_rate_limit(current_user.id, "app_info_reveal", REVEAL_LIMIT)

    # Decrypt the value before returning
    decrypted_value = app_info_ops.decrypt_entry_value(entry)
    return {"value": decrypted_value}


@router.post("/bulk", response_model=AppInfoBulkResponse, status_code=status.HTTP_201_CREATED)
async def bulk_create_app_info(
    data: AppInfoBulkCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """
    Bulk create app info entries from parsed .env content.

    Entries with duplicate keys (already existing in the product) are skipped.
    Duplicate keys within the request take the last occurrence.

    **Validation limits:**
    - Maximum 500 entries per request
    - Key length: max 255 characters
    - Value length: max 10,000 characters (10KB)
    - Maximum 10 tags per entry
    """
    await _check_variables_access(db, data.product_id, current_user.id)
    await require_product_subscription(db, data.product_id)

    # Validate bulk import limits
    if len(data.entries) > MAX_BULK_ENTRIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Too many entries. Maximum {MAX_BULK_ENTRIES} entries per request.",
        )

    # Validate default tags
    if data.default_tags:
        tag_errors = validate_tags(data.default_tags)
        if tag_errors:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid default tags: {'; '.join(tag_errors)}",
            )

    # Validate each entry
    validation_errors: list[str] = []
    for i, entry in enumerate(data.entries):
        if len(entry.key) > MAX_KEY_LENGTH:
            validation_errors.append(f"Entry {i}: key exceeds {MAX_KEY_LENGTH} characters")
        if len(entry.value) > MAX_VALUE_LENGTH:
            validation_errors.append(
                f"Entry {i} ({entry.key}): value exceeds {MAX_VALUE_LENGTH} characters"
            )
        # Check for empty keys
        if not entry.key.strip():
            validation_errors.append(f"Entry {i}: key cannot be empty")
        # Validate entry-specific tags
        if entry.tags:
            entry_tag_errors = validate_tags(entry.tags)
            if entry_tag_errors:
                validation_errors.append(f"Entry {i} ({entry.key}): {'; '.join(entry_tag_errors)}")

    if validation_errors:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Validation failed: {'; '.join(validation_errors[:5])}"
            + (
                f" and {len(validation_errors) - 5} more errors"
                if len(validation_errors) > 5
                else ""
            ),
        )

    created, skipped = await app_info_ops.bulk_create(
        db,
        user_id=current_user.id,
        product_id=data.product_id,
        entries=data.entries,
        default_tags=data.default_tags,
    )

    return AppInfoBulkResponse(
        created=[
            {
                "id": str(e.id),
                "key": e.key,
                "value": "********" if e.is_secret else e.value,
                "category": e.category,
                "is_secret": e.is_secret,
                "description": e.description,
                "target_file": e.target_file,
                "tags": e.tags or [],
                "product_id": str(e.product_id) if e.product_id else None,
                "created_at": e.created_at.isoformat(),
                "updated_at": e.updated_at.isoformat(),
            }
            for e in created
        ],
        skipped=skipped,
    )
