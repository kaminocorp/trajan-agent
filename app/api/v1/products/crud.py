"""Product CRUD operations: list, get, create, update, delete."""

import uuid as uuid_pkg

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    SubscriptionContext,
    check_product_admin_access,
    check_product_editor_access,
    check_subscription_active,
    get_current_user,
    get_db_with_rls,
    get_subscription_context,
    require_product_subscription,
)
from app.domain import product_ops
from app.domain.organization_operations import organization_ops
from app.domain.product_access_operations import product_access_ops
from app.models.product import ProductCreate, ProductUpdate
from app.models.user import User

router = APIRouter()


@router.get("/", response_model=list[dict])
async def list_products(
    skip: int = 0,
    limit: int = 100,
    org_id: uuid_pkg.UUID | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """
    List all products the current user has access to.

    Args:
        org_id: Optional organization ID to filter by. If provided, only returns
                products from that organization. If not provided, returns products
                from all organizations the user is a member of.

    Returns products filtered by their access level:
    - Org owners/admins: see all products in their orgs
    - Org members/viewers: only see products they have explicit access to
    """
    # Get organizations to query - either specific org or all user's orgs
    if org_id:
        # Verify user is a member of this org
        org_role = await organization_ops.get_member_role(db, org_id, current_user.id)
        if not org_role:
            # User is not a member of this org, return empty list
            return []
        org = await organization_ops.get(db, org_id)
        if not org:
            return []
        user_orgs = [org]
    else:
        user_orgs = await organization_ops.get_for_user(db, current_user.id)

    accessible_products = []
    products_by_org: dict[uuid_pkg.UUID, list] = {}

    for org in user_orgs:
        # Get user's role in this org
        org_role = await organization_ops.get_member_role(db, org.id, current_user.id)
        if not org_role:
            continue

        # Get all products in this org
        org_products = await product_ops.get_by_organization(db, org.id)
        if not org_products:
            continue

        # Bulk check access for all products in this org (eliminates N+1)
        product_ids = [p.id for p in org_products]
        access_map = await product_access_ops.get_effective_access_bulk(
            db, product_ids, current_user.id, org_role
        )

        # Filter to accessible products
        org_accessible = [p for p in org_products if access_map.get(p.id, "none") != "none"]
        accessible_products.extend(org_accessible)
        products_by_org[org.id] = org_accessible

    # Apply pagination
    paginated = accessible_products[skip : skip + limit]

    # Bulk fetch collaborator counts per org (eliminates N+1)
    collab_counts: dict[uuid_pkg.UUID, int] = {}
    for org_id in products_by_org:
        paginated_ids = [p.id for p in paginated if p.organization_id == org_id]
        if paginated_ids:
            org_counts = await product_access_ops.get_product_collaborators_count_bulk(
                db, paginated_ids, org_id
            )
            collab_counts.update(org_counts)

    # Build response with collaborator counts and top contributors
    result = []
    for p in paginated:
        collab_count = collab_counts.get(p.id, 0)

        # Extract top contributor from product_overview if available
        top_contributor = None
        if p.product_overview and isinstance(p.product_overview, dict):
            stats = p.product_overview.get("stats", {})
            top_contributors = stats.get("top_contributors", [])
            if top_contributors and len(top_contributors) > 0:
                top_contributor = top_contributors[0]

        # Extract lead_user info if assigned
        lead_user = None
        if p.lead_user:
            lead_user = {
                "id": str(p.lead_user.id),
                "email": p.lead_user.email,
                "display_name": p.lead_user.display_name,
                "avatar_url": p.lead_user.avatar_url,
            }

        result.append(
            {
                "id": str(p.id),
                "name": p.name,
                "description": p.description,
                "icon": p.icon,
                "color": p.color,
                "analysis_status": p.analysis_status,
                "created_at": p.created_at.isoformat(),
                "updated_at": p.updated_at.isoformat(),
                "collaborator_count": collab_count,
                "top_contributor": top_contributor,
                "lead_user_id": str(p.lead_user_id) if p.lead_user_id else None,
                "lead_user": lead_user,
                "repository_count": len(p.repositories),
                "work_item_count": len(p.work_items),
                "document_count": len(p.documents),
            }
        )

    return result


@router.get("/{product_id}")
async def get_product(
    product_id: uuid_pkg.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """Get a single product with all related entities.

    Access control: User must have at least viewer access to the product
    through their organization membership.
    """
    # Fetch product by ID (without user_id filter for org-based access)
    product = await product_ops.get_with_relations_by_id(db, id=product_id)
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found",
        )

    # Check organization membership and product access
    if not product.organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found",
        )

    org_role = await organization_ops.get_member_role(db, product.organization_id, current_user.id)
    if not org_role:
        # User is not a member of this product's organization
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found",
        )

    # Verify user has at least viewer access to this product
    access = await product_access_ops.get_effective_access(
        db, product_id, current_user.id, org_role
    )
    if access == "none":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found",
        )

    # Extract lead_user info if assigned
    lead_user = None
    if product.lead_user:
        lead_user = {
            "id": str(product.lead_user.id),
            "email": product.lead_user.email,
            "display_name": product.lead_user.display_name,
            "avatar_url": product.lead_user.avatar_url,
        }

    return {
        "id": str(product.id),
        "name": product.name,
        "description": product.description,
        "icon": product.icon,
        "color": product.color,
        "analysis_status": product.analysis_status,
        "analysis_error": product.analysis_error,
        "analysis_progress": product.analysis_progress,
        "product_overview": product.product_overview,
        "created_at": product.created_at.isoformat(),
        "updated_at": product.updated_at.isoformat(),
        "repositories_count": len(product.repositories),
        "work_items_count": len(product.work_items),
        "documents_count": len(product.documents),
        "lead_user_id": str(product.lead_user_id) if product.lead_user_id else None,
        "lead_user": lead_user,
    }


@router.post("/", status_code=status.HTTP_201_CREATED)
async def create_product(
    data: ProductCreate,
    current_user: User = Depends(get_current_user),
    sub_ctx: SubscriptionContext = Depends(get_subscription_context),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """Create a new product."""
    # Check subscription is active for user's current org
    # (product doesn't exist yet, so we use the user's org context)
    await check_subscription_active(sub_ctx, db)

    # Check for duplicate name
    existing = await product_ops.get_by_name(db, user_id=current_user.id, name=data.name)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Product with this name already exists",
        )

    # Include organization_id from subscription context
    product_data = data.model_dump()
    product_data["organization_id"] = sub_ctx.organization.id

    product = await product_ops.create(
        db,
        obj_in=product_data,
        user_id=current_user.id,
    )
    return {
        "id": str(product.id),
        "name": product.name,
        "description": product.description,
        "icon": product.icon,
        "color": product.color,
        "created_at": product.created_at.isoformat(),
        "updated_at": product.updated_at.isoformat(),
    }


@router.patch("/{product_id}")
async def update_product(
    product_id: uuid_pkg.UUID,
    data: ProductUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """Update a product. Requires Editor or Admin access."""
    await check_product_editor_access(db, product_id, current_user.id)
    sub_ctx = await require_product_subscription(db, product_id)
    product = sub_ctx.product

    updated = await product_ops.update(
        db, db_obj=product, obj_in=data.model_dump(exclude_unset=True)
    )
    return {
        "id": str(updated.id),
        "name": updated.name,
        "description": updated.description,
        "icon": updated.icon,
        "color": updated.color,
        "created_at": updated.created_at.isoformat(),
        "updated_at": updated.updated_at.isoformat(),
    }


@router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_product(
    product_id: uuid_pkg.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """Delete a product and all related entities. Requires Admin access."""
    await check_product_admin_access(db, product_id, current_user.id)
    sub_ctx = await require_product_subscription(db, product_id)
    product = sub_ctx.product
    await db.delete(product)
    await db.flush()
