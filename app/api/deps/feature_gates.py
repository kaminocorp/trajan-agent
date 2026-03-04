"""Subscription-based feature gating dependencies."""

import uuid as uuid_pkg
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from datetime import UTC, datetime

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.plans import PlanConfig, get_plan
from app.core.database import get_db
from app.domain.organization_operations import organization_ops
from app.domain.subscription_operations import subscription_ops
from app.models.organization import Organization
from app.models.subscription import Subscription, SubscriptionStatus

from .organization import get_current_organization

_PLAN_CONFIG_FIELDS = frozenset(f.name for f in dataclass_fields(PlanConfig))


@dataclass
class SubscriptionContext:
    """Context containing organization, subscription, and plan configuration."""

    organization: Organization
    subscription: Subscription
    plan: PlanConfig
    product: object | None = None  # Product when resolved via product-scoped context


async def get_subscription_context(
    current_org: Organization = Depends(get_current_organization),
    db: AsyncSession = Depends(get_db),
) -> SubscriptionContext:
    """
    Get the subscription context for the current organization.

    Returns org, subscription, and plan config for feature checks.
    """
    subscription = await subscription_ops.get_by_org(db, current_org.id)

    if not subscription:
        # Shouldn't happen if org was created properly, but handle gracefully
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Organization subscription not found",
        )

    plan = get_plan(subscription.plan_tier)

    return SubscriptionContext(
        organization=current_org,
        subscription=subscription,
        plan=plan,
    )


async def get_subscription_context_for_product(
    db: AsyncSession,
    product_id: uuid_pkg.UUID,
) -> SubscriptionContext:
    """
    Get subscription context for a product's organization.

    Use this when checking limits for operations on a specific product,
    rather than the user's default organization. This ensures that collaborators
    on a paid organization can use that org's subscription limits, even if their
    personal organization is on a free tier.

    Args:
        db: Database session
        product_id: The product whose organization's subscription to use

    Returns:
        SubscriptionContext for the product's organization

    Raises:
        HTTPException 404: Product or organization not found
        HTTPException 400: Product has no organization
        HTTPException 500: Organization has no subscription
    """
    from app.domain.product_operations import product_ops

    product = await product_ops.get(db, product_id)
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found",
        )

    if not product.organization_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Product has no organization",
        )

    org = await organization_ops.get(db, product.organization_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    subscription = await subscription_ops.get_by_org(db, org.id)
    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Organization subscription not found",
        )

    plan = get_plan(subscription.plan_tier)

    return SubscriptionContext(
        organization=org,
        subscription=subscription,
        plan=plan,
        product=product,
    )


class FeatureGate:
    """
    Dependency class for feature gating based on subscription plan.

    DEPRECATED: Uses get_subscription_context (user's default org), which is wrong
    for product-scoped endpoints. Not currently used on any v1 endpoint.
    If re-enabling, create a product-scoped variant that resolves from the product's org.

    Usage:
        @router.post("/some-feature")
        async def some_feature(
            _: bool = Depends(FeatureGate("drift_detection")),
            ...
        ):
            # Only accessible if plan has drift_detection feature
            ...
    """

    def __init__(self, feature: str):
        if feature not in _PLAN_CONFIG_FIELDS:
            raise ValueError(
                f"Unknown feature '{feature}' for FeatureGate. "
                f"Valid features: {sorted(_PLAN_CONFIG_FIELDS)}"
            )
        self.feature = feature

    async def __call__(
        self,
        ctx: SubscriptionContext = Depends(get_subscription_context),
    ) -> bool:
        """Check if the feature is enabled for the current organization's plan."""
        has_feature = getattr(ctx.plan, self.feature, False)

        if not has_feature:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Feature '{self.feature}' requires a higher plan. "
                f"Current plan: {ctx.plan.display_name}",
            )

        return True


async def check_subscription_active(
    ctx: SubscriptionContext, db: AsyncSession | None = None
) -> None:
    """Raise 402 if the subscription is pending, has no plan, or has expired.

    Use this to validate a pre-fetched SubscriptionContext (e.g., from
    get_subscription_context or get_subscription_context_for_product).

    When ``db`` is provided and the subscription is a manually assigned plan
    whose ``manual_assignment_expires_at`` is in the past, the subscription is
    reverted to no-plan inline and a 402 is raised.
    """
    sub = ctx.subscription

    # Check for expired manual assignment (beta expiry)
    if (
        db is not None
        and sub.is_manually_assigned
        and sub.manual_assignment_expires_at is not None
        and sub.manual_assignment_expires_at <= datetime.now(UTC)
    ):
        await subscription_ops.update(
            db,
            sub,
            {
                "plan_tier": "none",
                "status": SubscriptionStatus.PENDING.value,
                "is_manually_assigned": False,
                "manually_assigned_by": None,
                "manually_assigned_at": None,
                "manual_assignment_note": None,
                "manual_assignment_expires_at": None,
                "base_repo_limit": 1,
            },
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "code": "SUBSCRIPTION_REQUIRED",
                "message": "Your free Pro access has expired — please select a plan to continue",
            },
        )

    is_pending = (
        sub.plan_tier == "none"
        or sub.status == SubscriptionStatus.PENDING.value
    )
    if is_pending:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "code": "SUBSCRIPTION_REQUIRED",
                "message": "Please select a plan to continue",
            },
        )


async def require_active_subscription(
    ctx: SubscriptionContext = Depends(get_subscription_context),
    db: AsyncSession = Depends(get_db),
) -> SubscriptionContext:
    """
    Require an active (non-pending) subscription to access the app.

    Uses the user's DEFAULT organization. For product-scoped endpoints,
    use require_product_subscription() instead — it resolves the subscription
    from the product's organization, not the user's default.

    Returns 402 Payment Required with SUBSCRIPTION_REQUIRED code if pending.

    Exempt endpoints (billing, user profile) should NOT use this dependency.
    """
    await check_subscription_active(ctx, db)
    return ctx


async def require_product_subscription(
    db: AsyncSession,
    product_id: uuid_pkg.UUID,
) -> SubscriptionContext:
    """
    Require the product's organization to have an active subscription.

    Use this instead of require_active_subscription for product-scoped
    endpoints. Resolves the subscription from the product's organization,
    not the user's default organization — fixing incorrect 402s for
    multi-org users.

    Returns SubscriptionContext for further checks (repo limits, agent, etc.).
    Raises 402 if subscription is pending or has no plan.
    """
    ctx = await get_subscription_context_for_product(db, product_id)
    await check_subscription_active(ctx, db)
    return ctx


async def require_agent_enabled(
    ctx: SubscriptionContext = Depends(get_subscription_context),
    db: AsyncSession = Depends(get_db),
) -> SubscriptionContext:
    """
    Require that agent features are enabled for the organization.

    DEPRECATED: Uses get_subscription_context (user's default org), which is wrong
    for product-scoped endpoints. Not currently used on any v1 endpoint.
    Product-scoped agent checks are done inline (e.g., analysis.py uses
    subscription_ops.is_agent_enabled with the product's org context).

    For free tier: Checks if org is within repo limit.
    For paid tiers: Always enabled.

    Returns the subscription context if agent is enabled.
    """
    from app.domain.repository_operations import repository_ops

    # Count current repos for the org
    repo_count = await repository_ops.count_by_org(db, ctx.organization.id)

    is_enabled = await subscription_ops.is_agent_enabled(db, ctx.organization.id, repo_count)

    if not is_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Agent disabled. You have {repo_count} repositories but your "
            f"{ctx.plan.display_name} plan only allows {ctx.plan.base_repo_limit}. "
            "Remove repositories or upgrade to re-enable.",
        )

    return ctx
