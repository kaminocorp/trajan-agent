"""Repository API endpoints with product-scoped visibility.

Repositories are visible to all users with Product access.
- Viewers can list and view repositories
- Editors can create, update, and delete repositories
"""

import logging
import uuid as uuid_pkg

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    SubscriptionContext,
    check_product_editor_access,
    check_subscription_active,
    get_current_organization,
    get_current_user,
    get_db_with_rls,
    get_product_access,
    require_product_subscription,
)
from app.api.v1.products.analysis import maybe_auto_trigger_analysis
from app.api.v1.products.docs_generation import maybe_auto_trigger_docs
from app.config.plans import get_plan
from app.domain import repository_ops
from app.domain.subscription_operations import subscription_ops
from app.models.repository import RepositoryCreate, RepositoryUpdate
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/repositories", tags=["repositories"])


def _serialize_repository(r):
    """Serialize a repository to a dict response."""
    return {
        "id": str(r.id),
        "name": r.name,
        "full_name": r.full_name,
        "description": r.description,
        "url": r.url,
        "default_branch": r.default_branch,
        "is_private": r.is_private,
        "language": r.language,
        "github_id": r.github_id,
        "stars_count": r.stars_count,
        "forks_count": r.forks_count,
        "source_type": getattr(r, "source_type", "github"),
        "has_token": bool(getattr(r, "encrypted_token", None)),
        "product_id": str(r.product_id) if r.product_id else None,
        "imported_by_user_id": str(r.imported_by_user_id),
        "created_at": r.created_at.isoformat(),
        "updated_at": r.updated_at.isoformat(),
    }


@router.get("", response_model=list[dict])
async def list_repositories(
    product_id: uuid_pkg.UUID | None = Query(None, description="Filter by product"),
    skip: int = 0,
    limit: int = 100,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """List repositories, optionally filtered by product.

    If product_id is provided, returns all repos in that product (requires product access).
    RLS enforces that only repos in accessible products are returned.
    """
    if product_id:
        # Verify product access (will raise 403 if no access)
        await get_product_access(product_id, db, current_user)
        repos = await repository_ops.get_by_product(
            db, product_id=product_id, skip=skip, limit=limit
        )
    else:
        # Without product_id filter, RLS will only return repos from accessible products
        # For now, require product_id to avoid returning repos from all products
        repos = []

    return [_serialize_repository(r) for r in repos]


@router.get("/{repository_id}")
async def get_repository(
    repository_id: uuid_pkg.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """Get a single repository. Requires viewer access to the product."""
    repo = await repository_ops.get(db, id=repository_id)
    if not repo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repository not found",
        )

    # Verify product access (RLS should already filter, but explicit check is clearer)
    if repo.product_id:
        await get_product_access(repo.product_id, db, current_user)

    return _serialize_repository(repo)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_repository(
    data: RepositoryCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """
    Create a new repository.

    Repository limits are enforced based on subscription plan:
    - Free tier (Observer): Cannot exceed base limit
    - Paid tiers: Allowed to exceed with overage charges

    Subscription limits are checked against the TARGET organization:
    - If product_id is provided, uses the product's organization subscription
    - Otherwise, falls back to the user's default organization

    Requires Editor or Admin access to the product.
    """
    # Determine target organization for subscription limit check
    # IMPORTANT: Use product's org, not user's default org (fixes cross-org subscription bug)
    if data.product_id:
        # Check product access first
        await check_product_editor_access(db, data.product_id, current_user.id)
        # Get subscription context for the PRODUCT's organization + check active
        sub_ctx = await require_product_subscription(db, data.product_id)
    else:
        # Fallback: repo without a product uses user's default organization
        default_org = await get_current_organization(org_id=None, current_user=current_user, db=db)
        subscription = await subscription_ops.get_by_org(db, default_org.id)
        if not subscription:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Organization subscription not found",
            )
        sub_ctx = SubscriptionContext(
            organization=default_org,
            subscription=subscription,
            plan=get_plan(subscription.plan_tier),
        )
        await check_subscription_active(sub_ctx, db)

    # Check repo limit before creation
    current_count = await repository_ops.count_by_org(db, sub_ctx.organization.id)
    limit_status = await subscription_ops.check_repo_limit(
        db,
        organization_id=sub_ctx.organization.id,
        current_repo_count=current_count,
        additional_count=1,
    )

    if not limit_status.can_add:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Repository limit reached ({limit_status.base_limit}). "
            f"Upgrade your plan to add more repositories.",
        )

    # Warn about overage for paid plans (but still allow)
    # This is informational - actual billing happens via Stripe metered usage

    obj_data = data.model_dump()
    if obj_data.get("full_name") is None:
        obj_data["full_name"] = obj_data["name"]
    repo = await repository_ops.create(
        db,
        obj_in=obj_data,
        imported_by_user_id=current_user.id,
    )

    # Auto-trigger docs generation if user preference is enabled
    docs_triggered = False
    if data.product_id:
        try:
            docs_triggered = await maybe_auto_trigger_docs(
                product_id=data.product_id,
                user_id=current_user.id,
                db=db,
            )
        except Exception as e:
            logger.warning(
                f"Auto-trigger docs failed for product {data.product_id}: {e}. "
                "User can manually trigger docs generation."
            )

    # Auto-trigger project analysis if user preference is enabled
    analysis_triggered = False
    if data.product_id:
        try:
            analysis_triggered = await maybe_auto_trigger_analysis(
                product_id=data.product_id,
                user_id=current_user.id,
                db=db,
            )
        except Exception as e:
            logger.warning(
                f"Auto-trigger analysis failed for product {data.product_id}: {e}. "
                "User can manually trigger analysis."
            )

    result = _serialize_repository(repo)
    result["docs_generation_triggered"] = docs_triggered
    result["analysis_triggered"] = analysis_triggered
    return result


@router.patch("/{repository_id}")
async def update_repository(
    repository_id: uuid_pkg.UUID,
    data: RepositoryUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """Update a repository. Requires Editor or Admin access to the product."""
    repo = await repository_ops.get(db, id=repository_id)
    if not repo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repository not found",
        )

    # Repos must be associated with a product — orphaned repos cannot be modified
    if not repo.product_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Repository is not associated with a product",
        )

    # Check product access (editor required for updates)
    await check_product_editor_access(db, repo.product_id, current_user.id)
    await require_product_subscription(db, repo.product_id)

    updated = await repository_ops.update(
        db, db_obj=repo, obj_in=data.model_dump(exclude_unset=True)
    )
    return _serialize_repository(updated)


@router.delete("/{repository_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_repository(
    repository_id: uuid_pkg.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """Delete a repository. Requires Editor or Admin access to the product."""
    # Get repo first to check product access
    repo = await repository_ops.get(db, id=repository_id)
    if not repo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repository not found",
        )

    # Repos must be associated with a product — orphaned repos cannot be deleted
    if not repo.product_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Repository is not associated with a product",
        )

    # Check product access (editor required for deletion)
    await check_product_editor_access(db, repo.product_id, current_user.id)
    await require_product_subscription(db, repo.product_id)

    await repository_ops.delete(db, id=repository_id)
