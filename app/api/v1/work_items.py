import uuid as uuid_pkg

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    check_product_editor_access,
    get_current_user,
    get_db_with_rls,
    get_product_access,
    require_product_subscription,
)
from app.domain import work_item_ops
from app.models.user import User
from app.models.work_item import WorkItem, WorkItemComplete, WorkItemCreate, WorkItemUpdate

router = APIRouter(prefix="/work-items", tags=["work items"])


def _serialize_work_item(item: WorkItem) -> dict:
    """Serialize a work item to a dict response."""
    return {
        "id": str(item.id),
        "title": item.title,
        "description": item.description,
        "type": item.type,
        "status": item.status,
        "priority": item.priority,
        "product_id": str(item.product_id) if item.product_id else None,
        "repository_id": str(item.repository_id) if item.repository_id else None,
        "created_by_user_id": str(item.created_by_user_id),
        "created_at": item.created_at.isoformat(),
        "updated_at": item.updated_at.isoformat(),
        "completed_at": item.completed_at.isoformat() if item.completed_at else None,
        "commit_sha": item.commit_sha,
        "commit_url": item.commit_url,
        "plans": item.plans,
        "tags": item.tags,
        "deleted_at": item.deleted_at.isoformat() if item.deleted_at else None,
        "source": item.source,
        "reporter_email": item.reporter_email,
        "reporter_name": item.reporter_name,
    }


@router.get("", response_model=list[dict])
async def list_work_items(
    product_id: uuid_pkg.UUID = Query(..., description="Product ID (required)"),
    status: str | None = Query(None, description="Filter by status"),
    type: str | None = Query(None, description="Filter by type"),
    skip: int = 0,
    limit: int = 100,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """List work items for a product. Requires product access."""
    # Verify product access (viewer level is sufficient for reading)
    await get_product_access(product_id, db, current_user)

    items = await work_item_ops.get_by_product(
        db,
        product_id=product_id,
        status=status,
        type=type,
        skip=skip,
        limit=limit,
    )
    return [_serialize_work_item(w) for w in items]


@router.get("/all", response_model=list[dict])
async def list_all_work_items(
    status: str | None = Query(None, description="Filter by status"),
    product_id: uuid_pkg.UUID | None = Query(None, description="Filter by product"),
    org_id: uuid_pkg.UUID | None = Query(None, description="Filter by organization"),
    _current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """List all work items accessible to the user across products.

    RLS restricts results to products the user can access.
    When org_id is provided, only returns work items for that organization.
    Excludes soft-deleted items.
    """
    items = await work_item_ops.get_all_accessible(
        db,
        status=status,
        product_id=product_id,
        org_id=org_id,
    )
    return [_serialize_work_item(w) for w in items]


@router.get("/{work_item_id}")
async def get_work_item(
    work_item_id: uuid_pkg.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """Get a single work item. Requires product access."""
    item = await work_item_ops.get(db, work_item_id=work_item_id)
    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Work item not found",
        )

    # Verify product access
    if item.product_id:
        await get_product_access(item.product_id, db, current_user)

    return _serialize_work_item(item)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_work_item(
    data: WorkItemCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """Create a new work item. Requires Editor or Admin access to the product."""
    # Check product access (editor level required for creation)
    if data.product_id:
        await check_product_editor_access(db, data.product_id, current_user.id)
        await require_product_subscription(db, data.product_id)

    item = await work_item_ops.create(
        db,
        obj_in=data.model_dump(),
        created_by_user_id=current_user.id,
    )
    return _serialize_work_item(item)


@router.patch("/{work_item_id}/complete")
async def complete_work_item(
    work_item_id: uuid_pkg.UUID,
    body: WorkItemComplete,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """Complete a work item and link a commit. Requires Editor or Admin access."""
    item = await work_item_ops.get(db, work_item_id=work_item_id)
    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Work item not found",
        )

    if item.product_id:
        await check_product_editor_access(db, item.product_id, current_user.id)
        await require_product_subscription(db, item.product_id)

    completed = await work_item_ops.complete(
        db, work_item=item, commit_sha=body.commit_sha, commit_url=body.commit_url
    )
    return _serialize_work_item(completed)


@router.patch("/{work_item_id}")
async def update_work_item(
    work_item_id: uuid_pkg.UUID,
    data: WorkItemUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """Update a work item. Requires Editor or Admin access to the product."""
    item = await work_item_ops.get(db, work_item_id=work_item_id)
    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Work item not found",
        )

    # Check product access (editor level required for update)
    if item.product_id:
        await check_product_editor_access(db, item.product_id, current_user.id)
        await require_product_subscription(db, item.product_id)

    updated = await work_item_ops.update(
        db, db_obj=item, obj_in=data.model_dump(exclude_unset=True)
    )
    return _serialize_work_item(updated)


@router.delete("/{work_item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_work_item(
    work_item_id: uuid_pkg.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """Delete a work item. Requires Editor or Admin access to the product."""
    # Get work item first to check product access
    item = await work_item_ops.get(db, work_item_id=work_item_id)
    if not item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Work item not found",
        )

    # Check product access (editor level required for deletion)
    if item.product_id:
        await check_product_editor_access(db, item.product_id, current_user.id)
        await require_product_subscription(db, item.product_id)

    await work_item_ops.delete(db, work_item=item)
