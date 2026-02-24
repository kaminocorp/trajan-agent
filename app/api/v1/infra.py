import uuid as uuid_pkg

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    check_product_editor_access,
    check_product_viewer_access,
    get_current_user,
    get_db_with_rls,
    require_product_subscription,
)
from app.domain import infra_component_ops
from app.domain.infra_component_operations import VALID_COMPONENT_TYPES
from app.models.infra_component import InfraComponent, InfraComponentCreate, InfraComponentUpdate
from app.models.user import User

router = APIRouter(prefix="/infra", tags=["infra"])


def _serialize_component(c: InfraComponent) -> dict:
    """Serialize an infra component to a dict response."""
    return {
        "id": str(c.id),
        "product_id": str(c.product_id) if c.product_id else None,
        "name": c.name,
        "component_type": c.component_type,
        "provider": c.provider,
        "url": c.url,
        "description": c.description,
        "region": c.region,
        "metadata": c.metadata_,
        "display_order": c.display_order,
        "created_at": c.created_at.isoformat(),
        "updated_at": c.updated_at.isoformat(),
    }


@router.get("", response_model=list[dict])
async def list_infra_components(
    product_id: uuid_pkg.UUID = Query(..., description="Product ID"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """List all infrastructure components for a product."""
    await check_product_viewer_access(db, product_id, current_user.id)

    components = await infra_component_ops.get_by_product(db, product_id)
    return [_serialize_component(c) for c in components]


@router.get("/{component_id}")
async def get_infra_component(
    component_id: uuid_pkg.UUID,
    product_id: uuid_pkg.UUID = Query(..., description="Product ID"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """Get a single infrastructure component."""
    await check_product_viewer_access(db, product_id, current_user.id)

    component = await infra_component_ops.get_by_id_for_product(db, product_id, component_id)
    if not component:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Infrastructure component not found",
        )
    return _serialize_component(component)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_infra_component(
    data: InfraComponentCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """Create a new infrastructure component."""
    await check_product_editor_access(db, data.product_id, current_user.id)
    await require_product_subscription(db, data.product_id)

    # Validate component_type
    if data.component_type not in VALID_COMPONENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid component_type '{data.component_type}'. "
            f"Must be one of: {', '.join(sorted(VALID_COMPONENT_TYPES))}",
        )

    component = await infra_component_ops.create(
        db,
        obj_in=data.model_dump(by_alias=True),
        user_id=current_user.id,
    )
    return _serialize_component(component)


@router.patch("/{component_id}")
async def update_infra_component(
    component_id: uuid_pkg.UUID,
    data: InfraComponentUpdate,
    product_id: uuid_pkg.UUID = Query(..., description="Product ID"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """Update an infrastructure component."""
    await check_product_editor_access(db, product_id, current_user.id)
    await require_product_subscription(db, product_id)

    component = await infra_component_ops.get_by_id_for_product(db, product_id, component_id)
    if not component:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Infrastructure component not found",
        )

    # Validate component_type if being updated
    update_data = data.model_dump(exclude_unset=True, by_alias=True)
    if (
        "component_type" in update_data
        and update_data["component_type"] not in VALID_COMPONENT_TYPES
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid component_type '{update_data['component_type']}'. "
            f"Must be one of: {', '.join(sorted(VALID_COMPONENT_TYPES))}",
        )

    updated = await infra_component_ops.update(db, db_obj=component, obj_in=update_data)
    return _serialize_component(updated)


@router.delete("/{component_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_infra_component(
    component_id: uuid_pkg.UUID,
    product_id: uuid_pkg.UUID = Query(..., description="Product ID"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """Delete an infrastructure component."""
    await check_product_editor_access(db, product_id, current_user.id)
    await require_product_subscription(db, product_id)

    component = await infra_component_ops.get_by_id_for_product(db, product_id, component_id)
    if not component:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Infrastructure component not found",
        )

    await infra_component_ops.delete(db, id=component_id, user_id=current_user.id)
