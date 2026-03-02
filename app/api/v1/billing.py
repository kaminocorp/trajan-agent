"""Billing API endpoints for subscription management via Stripe."""

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.config import settings
from app.config.plans import PLANS, get_plan
from app.domain.discount_operations import discount_ops
from app.domain.organization_operations import organization_ops
from app.domain.referral_operations import referral_ops
from app.domain.repository_operations import repository_ops
from app.domain.subscription_operations import subscription_ops
from app.models.billing import BillingEvent, BillingEventType
from app.models.subscription import PlanTier, SubscriptionStatus
from app.services.stripe_service import stripe_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/billing", tags=["billing"])


# ─────────────────────────────────────────────────────────────────────────────
# Request/Response Schemas
# ─────────────────────────────────────────────────────────────────────────────


class PlanInfo(BaseModel):
    """Public plan information."""

    tier: str
    display_name: str
    price_monthly: int  # cents
    base_repo_limit: int
    overage_price: int  # cents per repo
    features: dict[str, bool]


class SubscriptionInfo(BaseModel):
    """Subscription information for the current organization."""

    plan_tier: str
    display_name: str
    status: str
    base_repo_limit: int
    current_period_end: str | None
    cancel_at_period_end: bool
    is_manually_assigned: bool
    is_trialing: bool


class CheckoutRequest(BaseModel):
    """Request to create a checkout session."""

    plan_tier: str
    organization_id: UUID
    source: str = "billing"  # "billing", "select-plan", or "onboarding" — determines redirect URLs
    discount_code: str | None = None  # Optional discount code to apply at checkout


class CheckoutResponse(BaseModel):
    """Response with checkout URL."""

    checkout_url: str


class PortalRequest(BaseModel):
    """Request to create a portal session."""

    organization_id: UUID


class PortalResponse(BaseModel):
    """Response with portal URL."""

    portal_url: str


class CancelRequest(BaseModel):
    """Request to cancel a subscription at period end."""

    organization_id: UUID


class CancelResponse(BaseModel):
    """Response after scheduling cancellation."""

    message: str
    cancel_at: str  # ISO date when subscription ends


class ReactivateRequest(BaseModel):
    """Request to reactivate a subscription that was set to cancel."""

    organization_id: UUID


class ReactivateResponse(BaseModel):
    """Response after reactivating subscription."""

    message: str


class DowngradeRequest(BaseModel):
    """Request to downgrade to a lower plan tier."""

    organization_id: UUID
    target_plan_tier: str
    repos_to_keep: list[UUID]  # IDs of repositories to keep; others will be deleted


class DowngradeResponse(BaseModel):
    """Response after successful downgrade."""

    success: bool
    message: str
    deleted_repo_count: int
    overage_repo_count: int = 0
    monthly_overage_cost: int = 0  # in cents


class ApplyDiscountRequest(BaseModel):
    """Request to apply a discount code."""

    organization_id: UUID
    code: str


class ApplyDiscountResponse(BaseModel):
    """Response after applying a discount code."""

    message: str
    discount_percent: int
    code: str


class DiscountInfoResponse(BaseModel):
    """Active discount information for an organization."""

    code: str
    discount_percent: int
    redeemed_at: str


class ValidateDiscountRequest(BaseModel):
    """Request to validate a discount code (without redeeming)."""

    code: str


class ValidateDiscountResponse(BaseModel):
    """Response with discount code validation result."""

    valid: bool
    discount_percent: int
    code: str


class RemoveDiscountRequest(BaseModel):
    """Request to remove an active discount."""

    organization_id: UUID


# ─────────────────────────────────────────────────────────────────────────────
# Public Endpoints
# ─────────────────────────────────────────────────────────────────────────────


@router.get("/plans", response_model=list[PlanInfo])
async def list_plans() -> list[PlanInfo]:
    """
    List all available plans (public endpoint).

    Returns plan details including pricing and feature flags.
    No authentication required.
    """
    return [
        PlanInfo(
            tier=plan.tier,
            display_name=plan.display_name,
            price_monthly=plan.price_monthly,
            base_repo_limit=plan.base_repo_limit,
            overage_price=plan.overage_repo_price,
            features=plan.features,
        )
        for plan in PLANS.values()
    ]


# ─────────────────────────────────────────────────────────────────────────────
# Authenticated Endpoints
# ─────────────────────────────────────────────────────────────────────────────


@router.get("/subscription/{organization_id}", response_model=SubscriptionInfo)
async def get_subscription(
    organization_id: UUID,
    db: DbSession,
    current_user: CurrentUser,
) -> SubscriptionInfo:
    """
    Get subscription details for an organization.

    User must be a member of the organization.
    """
    # Verify user has access to this org
    role = await organization_ops.get_member_role(db, organization_id, current_user.id)
    if not role:
        raise HTTPException(403, "Not a member of this organization")

    subscription = await subscription_ops.get_by_org(db, organization_id)
    if not subscription:
        raise HTTPException(404, "Subscription not found")

    plan = get_plan(subscription.plan_tier)

    return SubscriptionInfo(
        plan_tier=subscription.plan_tier,
        display_name=plan.display_name,
        status=subscription.status,
        base_repo_limit=subscription.base_repo_limit,
        current_period_end=(
            subscription.current_period_end.isoformat() if subscription.current_period_end else None
        ),
        cancel_at_period_end=subscription.cancel_at_period_end,
        is_manually_assigned=subscription.is_manually_assigned,
        is_trialing=subscription.status == SubscriptionStatus.TRIALING.value,
    )


@router.post("/checkout", response_model=CheckoutResponse)
async def create_checkout(
    request: CheckoutRequest,
    db: DbSession,
    current_user: CurrentUser,
) -> CheckoutResponse:
    """
    Create a Stripe Checkout session for plan subscription.

    User must be owner or admin of the organization.
    Returns a URL to redirect the user to Stripe Checkout.
    Includes a 14-day free trial for first-time subscribers only.
    """
    if not settings.stripe_enabled:
        raise HTTPException(400, "Payments not configured")

    # Verify user is owner/admin
    role = await organization_ops.get_member_role(db, request.organization_id, current_user.id)
    if not role or role not in ("owner", "admin"):
        raise HTTPException(403, "Only owners and admins can manage billing")

    # Validate plan tier
    valid_tiers = [PlanTier.INDIE.value, PlanTier.PRO.value, PlanTier.SCALE.value]
    if request.plan_tier not in valid_tiers:
        raise HTTPException(400, f"Invalid plan tier. Must be one of: {valid_tiers}")

    # Get subscription
    subscription = await subscription_ops.get_by_org(db, request.organization_id)
    if not subscription:
        raise HTTPException(404, "Subscription not found")

    # Check if manually assigned (can't use Stripe checkout)
    if subscription.is_manually_assigned:
        raise HTTPException(400, "Subscription is manually managed — contact support to change")

    # Get or create Stripe customer
    org = await organization_ops.get(db, request.organization_id)
    if not org:
        raise HTTPException(404, "Organization not found")

    if not subscription.stripe_customer_id:
        customer_id = stripe_service.create_customer(org, current_user)
        await subscription_ops.update(db, subscription, {"stripe_customer_id": customer_id})
        await db.commit()
    else:
        customer_id = subscription.stripe_customer_id

    # Determine redirect URLs based on source
    if request.source == "onboarding":
        # User selecting plan during onboarding wizard — return to onboarding flow
        success_url = f"{settings.frontend_url}/onboarding?checkout=success&step=invite"
        cancel_url = f"{settings.frontend_url}/onboarding?checkout=canceled&step=plan"
    elif request.source == "select-plan":
        # New user selecting a plan (standalone page) — redirect to dashboard on success
        success_url = f"{settings.frontend_url}/dashboard?checkout=success"
        cancel_url = f"{settings.frontend_url}/select-plan?checkout=canceled"
    else:
        # Existing user upgrading from billing settings
        success_url = f"{settings.frontend_url}/settings/billing?success=true"
        cancel_url = f"{settings.frontend_url}/settings/billing?canceled=true"

    # Only grant a free trial if this org has never subscribed before
    include_trial = subscription.first_subscribed_at is None

    # Validate discount code and resolve Stripe coupon (if provided)
    coupon_id: str | None = None
    discount_code_value: str | None = None
    if request.discount_code:
        try:
            discount = await discount_ops.validate_code(db, request.discount_code)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None

        try:
            coupon_id = stripe_service.get_or_create_discount_coupon(
                discount.code, discount.discount_percent
            )
            discount_code_value = discount.code
        except Exception as e:
            logger.error(f"Failed to create Stripe coupon for checkout: {e}")
            raise HTTPException(500, "Failed to prepare discount") from None

    checkout_url = stripe_service.create_checkout_session(
        customer_id=customer_id,
        plan_tier=request.plan_tier,
        success_url=success_url,
        cancel_url=cancel_url,
        include_trial=include_trial,
        coupon_id=coupon_id,
        discount_code=discount_code_value,
    )

    return CheckoutResponse(checkout_url=checkout_url)


@router.post("/portal", response_model=PortalResponse)
async def create_portal(
    request: PortalRequest,
    db: DbSession,
    current_user: CurrentUser,
) -> PortalResponse:
    """
    Create a Stripe Customer Portal session for self-service billing.

    User must be owner or admin of the organization.
    Returns a URL to redirect the user to Stripe Portal.
    """
    if not settings.stripe_enabled:
        raise HTTPException(400, "Payments not configured")

    # Verify user is owner/admin
    role = await organization_ops.get_member_role(db, request.organization_id, current_user.id)
    if not role or role not in ("owner", "admin"):
        raise HTTPException(403, "Only owners and admins can manage billing")

    # Get subscription
    subscription = await subscription_ops.get_by_org(db, request.organization_id)
    if not subscription or not subscription.stripe_customer_id:
        raise HTTPException(400, "No Stripe customer — subscribe first")

    # Create portal session
    return_url = f"{settings.frontend_url}/settings/billing"
    portal_url = stripe_service.create_portal_session(
        customer_id=subscription.stripe_customer_id,
        return_url=return_url,
    )

    return PortalResponse(portal_url=portal_url)


@router.post("/cancel", response_model=CancelResponse)
async def cancel_subscription(
    request: CancelRequest,
    db: DbSession,
    current_user: CurrentUser,
) -> CancelResponse:
    """
    Cancel a subscription at the end of the current billing period.

    User must be owner or admin of the organization.
    The subscription remains active until the period end, then the organization
    and all its data will be permanently deleted.
    """
    if not settings.stripe_enabled:
        raise HTTPException(400, "Payments not configured")

    # Verify user is owner/admin
    role = await organization_ops.get_member_role(db, request.organization_id, current_user.id)
    if not role or role not in ("owner", "admin"):
        raise HTTPException(403, "Only owners and admins can manage billing")

    # Get subscription
    subscription = await subscription_ops.get_by_org(db, request.organization_id)
    if not subscription:
        raise HTTPException(404, "Subscription not found")

    # Check if manually assigned (can't cancel via API)
    if subscription.is_manually_assigned:
        raise HTTPException(400, "Subscription is manually managed — contact support to cancel")

    # Check if already canceling
    if subscription.cancel_at_period_end:
        raise HTTPException(400, "Subscription is already scheduled for cancellation")

    # Must have a Stripe subscription to cancel
    if not subscription.stripe_subscription_id:
        raise HTTPException(400, "No active Stripe subscription to cancel")

    # Cancel in Stripe (at period end)
    stripe_service.cancel_subscription(subscription.stripe_subscription_id)

    # Update local subscription record
    await subscription_ops.update(
        db,
        subscription,
        {"cancel_at_period_end": True},
    )

    # Log billing event
    await subscription_ops.log_event(
        db,
        organization_id=subscription.organization_id,
        event_type=BillingEventType.SUBSCRIPTION_UPDATED,
        new_value={"cancel_at_period_end": True},
        description="Subscription scheduled for cancellation at period end",
    )

    await db.commit()

    # Get period end date for response
    cancel_at = (
        subscription.current_period_end.isoformat()
        if subscription.current_period_end
        else datetime.now(UTC).isoformat()
    )

    logger.info(f"Subscription canceled for org {subscription.organization_id}, ends {cancel_at}")

    return CancelResponse(
        message="Subscription will be canceled at the end of the billing period",
        cancel_at=cancel_at,
    )


@router.post("/reactivate", response_model=ReactivateResponse)
async def reactivate_subscription(
    request: ReactivateRequest,
    db: DbSession,
    current_user: CurrentUser,
) -> ReactivateResponse:
    """
    Reactivate a subscription that was set to cancel at period end.

    User must be owner or admin of the organization.
    Removes the pending cancellation and the subscription continues normally.
    """
    if not settings.stripe_enabled:
        raise HTTPException(400, "Payments not configured")

    # Verify user is owner/admin
    role = await organization_ops.get_member_role(db, request.organization_id, current_user.id)
    if not role or role not in ("owner", "admin"):
        raise HTTPException(403, "Only owners and admins can manage billing")

    # Get subscription
    subscription = await subscription_ops.get_by_org(db, request.organization_id)
    if not subscription:
        raise HTTPException(404, "Subscription not found")

    # Check if manually assigned
    if subscription.is_manually_assigned:
        raise HTTPException(400, "Subscription is manually managed — contact support")

    # Check if actually pending cancellation
    if not subscription.cancel_at_period_end:
        raise HTTPException(400, "Subscription is not scheduled for cancellation")

    # Must have a Stripe subscription to reactivate
    if not subscription.stripe_subscription_id:
        raise HTTPException(400, "No active Stripe subscription")

    # Reactivate in Stripe
    stripe_service.reactivate_subscription(subscription.stripe_subscription_id)

    # Update local subscription record
    await subscription_ops.update(
        db,
        subscription,
        {"cancel_at_period_end": False},
    )

    # Log billing event
    await subscription_ops.log_event(
        db,
        organization_id=subscription.organization_id,
        event_type=BillingEventType.SUBSCRIPTION_UPDATED,
        new_value={"cancel_at_period_end": False},
        description="Subscription reactivated (cancellation removed)",
    )

    await db.commit()

    logger.info(f"Subscription reactivated for org {subscription.organization_id}")

    return ReactivateResponse(message="Subscription reactivated successfully")


@router.post("/downgrade", response_model=DowngradeResponse)
async def downgrade_subscription(
    request: DowngradeRequest,
    db: DbSession,
    current_user: CurrentUser,
) -> DowngradeResponse:
    """
    Downgrade a subscription to a lower plan tier.

    User must be owner or admin of the organization.

    If the target plan has fewer repo slots than current usage, the caller must
    specify which repos to keep. Repos not in the keep list will be deleted.

    Steps:
    1. Validate user permissions and plan tier
    2. Validate repos_to_keep matches target plan limit (if repo reduction needed)
    3. Delete repositories not in the keep list
    4. Change Stripe subscription to new plan
    5. Update local subscription record
    """
    if not settings.stripe_enabled:
        raise HTTPException(400, "Payments not configured")

    # Verify user is owner/admin
    role = await organization_ops.get_member_role(db, request.organization_id, current_user.id)
    if not role or role not in ("owner", "admin"):
        raise HTTPException(403, "Only owners and admins can manage billing")

    # Validate plan tier is a valid downgrade target
    valid_tiers = [PlanTier.INDIE.value, PlanTier.PRO.value, PlanTier.SCALE.value]
    if request.target_plan_tier not in valid_tiers:
        raise HTTPException(400, f"Invalid plan tier. Must be one of: {valid_tiers}")

    target_plan = get_plan(request.target_plan_tier)

    # Get current subscription
    subscription = await subscription_ops.get_by_org(db, request.organization_id)
    if not subscription:
        raise HTTPException(404, "Subscription not found")

    # Check if manually assigned (can't use Stripe)
    if subscription.is_manually_assigned:
        raise HTTPException(400, "Subscription is manually managed — contact support to change")

    # Must have a Stripe subscription
    if not subscription.stripe_subscription_id:
        raise HTTPException(400, "No active Stripe subscription")

    # Verify this is actually a downgrade
    current_plan = get_plan(subscription.plan_tier)
    plan_order = {"indie": 1, "pro": 2, "scale": 3}
    if plan_order.get(request.target_plan_tier, 0) >= plan_order.get(subscription.plan_tier, 0):
        raise HTTPException(400, "This is not a downgrade. Use checkout for upgrades.")

    # Get current repo count
    current_repo_count = await repository_ops.count_by_org(db, request.organization_id)

    # Validate repos_to_keep if reduction is needed
    deleted_count = 0
    if target_plan.allows_overages:
        # Plans with overages: user can keep all repos (empty list = keep all)
        # or optionally select repos to trim
        needs_repo_deletion = (
            len(request.repos_to_keep) > 0
            and current_repo_count > len(request.repos_to_keep)
        )
        if needs_repo_deletion and len(request.repos_to_keep) < 1:
            raise HTTPException(400, "Must keep at least 1 repository.")
    else:
        # Plans without overages (Indie): must trim to base limit
        needs_repo_deletion = current_repo_count > target_plan.base_repo_limit
        if needs_repo_deletion and len(request.repos_to_keep) != target_plan.base_repo_limit:
            raise HTTPException(
                400,
                f"Must specify exactly {target_plan.base_repo_limit} repositories to keep. "
                f"Got {len(request.repos_to_keep)}.",
            )

    # Change Stripe subscription plan FIRST (external side effect before local mutations).
    # If Stripe fails, no local data has been changed — nothing to roll back.
    try:
        stripe_service.change_subscription_plan(
            subscription.stripe_subscription_id, request.target_plan_tier
        )
    except Exception as e:
        logger.error(f"Stripe plan change failed: {e}")
        raise HTTPException(500, "Failed to update subscription in Stripe") from None

    # Delete repos AFTER Stripe succeeds. If deletion fails, the user is on the
    # correct plan and deletion can be retried.
    if needs_repo_deletion:
        deleted_count = await repository_ops.bulk_delete_except(
            db, request.organization_id, request.repos_to_keep
        )

    # Report overage usage if keeping repos above base limit
    final_repo_count = current_repo_count - deleted_count
    if final_repo_count > target_plan.base_repo_limit and target_plan.allows_overages:
        stripe_service.report_repo_usage(subscription, final_repo_count)

    # Update local subscription record
    previous_tier = subscription.plan_tier
    await subscription_ops.update(
        db,
        subscription,
        {
            "plan_tier": request.target_plan_tier,
            "base_repo_limit": target_plan.base_repo_limit,
        },
    )

    # Log billing event
    await subscription_ops.log_event(
        db,
        organization_id=subscription.organization_id,
        event_type=BillingEventType.PLAN_CHANGED,
        previous_value={"plan_tier": previous_tier, "repo_limit": current_plan.base_repo_limit},
        new_value={
            "plan_tier": request.target_plan_tier,
            "repo_limit": target_plan.base_repo_limit,
            "repos_deleted": deleted_count,
            "overage_repos": max(0, final_repo_count - target_plan.base_repo_limit),
            "overage_cost_cents": max(0, final_repo_count - target_plan.base_repo_limit)
            * target_plan.overage_repo_price,
        },
        description=f"Downgraded from {current_plan.display_name} to {target_plan.display_name}",
    )

    await db.commit()

    logger.info(
        f"Downgraded org {subscription.organization_id} from {previous_tier} to "
        f"{request.target_plan_tier}, deleted {deleted_count} repos"
    )

    overage_repos = max(0, final_repo_count - target_plan.base_repo_limit)
    overage_cost = overage_repos * target_plan.overage_repo_price

    return DowngradeResponse(
        success=True,
        message=f"Successfully downgraded to {target_plan.display_name}.",
        deleted_repo_count=deleted_count,
        overage_repo_count=overage_repos,
        monthly_overage_cost=overage_cost,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Discount Code Endpoints
# ─────────────────────────────────────────────────────────────────────────────


@router.post("/validate-discount", response_model=ValidateDiscountResponse)
async def validate_discount(
    request: ValidateDiscountRequest,
    db: DbSession,
    _current_user: CurrentUser,
) -> ValidateDiscountResponse:
    """
    Validate a discount code without redeeming it.

    Returns the code and discount percentage if valid.
    Used for live preview on plan selection pages.
    """
    try:
        discount = await discount_ops.validate_code(db, request.code)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None

    return ValidateDiscountResponse(
        valid=True,
        discount_percent=discount.discount_percent,
        code=discount.code,
    )


@router.post("/apply-discount", response_model=ApplyDiscountResponse)
async def apply_discount(
    request: ApplyDiscountRequest,
    db: DbSession,
    current_user: CurrentUser,
) -> ApplyDiscountResponse:
    """
    Redeem a discount code for an organization.

    User must be owner or admin of the organization.
    Creates or retrieves the Stripe coupon and applies it to the subscription.
    One active discount per organization.
    """
    # Verify user is owner/admin
    role = await organization_ops.get_member_role(db, request.organization_id, current_user.id)
    if not role or role not in ("owner", "admin"):
        raise HTTPException(403, "Only owners and admins can manage billing")

    # Get subscription — need a Stripe subscription to apply discount
    subscription = await subscription_ops.get_by_org(db, request.organization_id)
    if not subscription:
        raise HTTPException(404, "Subscription not found")

    if subscription.is_manually_assigned:
        raise HTTPException(400, "Subscription is manually managed — discounts not applicable")

    if not subscription.stripe_subscription_id:
        raise HTTPException(400, "Subscribe to a plan first")

    # Validate and redeem the code
    try:
        discount_code = await discount_ops.validate_code(db, request.code)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None

    # Create/retrieve Stripe coupon and apply to subscription
    try:
        coupon_id = stripe_service.get_or_create_discount_coupon(
            discount_code.code, discount_code.discount_percent
        )
    except Exception as e:
        logger.error(f"Failed to create Stripe coupon: {e}")
        raise HTTPException(500, "Failed to create discount in Stripe") from None

    applied = stripe_service.apply_discount_to_subscription(
        subscription.stripe_subscription_id, coupon_id
    )
    if not applied:
        raise HTTPException(500, "Failed to apply discount to subscription")

    # Record the redemption and update stripe_coupon_id if needed
    try:
        await discount_ops.redeem_code(
            db, request.code, request.organization_id, current_user.id
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from None

    if not discount_code.stripe_coupon_id:
        discount_code.stripe_coupon_id = coupon_id
        db.add(discount_code)

    # Log billing event
    await subscription_ops.log_event(
        db,
        organization_id=request.organization_id,
        event_type=BillingEventType.DISCOUNT_APPLIED,
        new_value={
            "code": discount_code.code,
            "discount_percent": discount_code.discount_percent,
            "coupon_id": coupon_id,
        },
        description=f"Discount code {discount_code.code} applied ({discount_code.discount_percent}% off)",
        actor_user_id=current_user.id,
    )

    await db.commit()

    logger.info(
        f"Applied discount {discount_code.code} ({discount_code.discount_percent}% off) "
        f"to org {request.organization_id}"
    )

    return ApplyDiscountResponse(
        message=f"{discount_code.discount_percent}% discount applied",
        discount_percent=discount_code.discount_percent,
        code=discount_code.code,
    )


@router.get("/discount/{organization_id}", response_model=DiscountInfoResponse | None)
async def get_discount(
    organization_id: UUID,
    db: DbSession,
    current_user: CurrentUser,
) -> DiscountInfoResponse | None:
    """
    Get the active discount for an organization.

    Returns the discount info or null if no discount is active.
    """
    # Verify user has access to this org
    role = await organization_ops.get_member_role(db, organization_id, current_user.id)
    if not role:
        raise HTTPException(403, "Not a member of this organization")

    redemption = await discount_ops.get_active_discount_for_org(db, organization_id)
    if not redemption or not redemption.discount_code:
        return None

    return DiscountInfoResponse(
        code=redemption.discount_code.code,
        discount_percent=redemption.discount_code.discount_percent,
        redeemed_at=redemption.redeemed_at.isoformat(),
    )


@router.post("/remove-discount")
async def remove_discount(
    request: RemoveDiscountRequest,
    db: DbSession,
    current_user: CurrentUser,
) -> dict[str, str]:
    """
    Remove the active discount from an organization's subscription.

    User must be owner or admin of the organization.
    """
    # Verify user is owner/admin
    role = await organization_ops.get_member_role(db, request.organization_id, current_user.id)
    if not role or role not in ("owner", "admin"):
        raise HTTPException(403, "Only owners and admins can manage billing")

    # Get subscription
    subscription = await subscription_ops.get_by_org(db, request.organization_id)
    if not subscription or not subscription.stripe_subscription_id:
        raise HTTPException(400, "No active subscription")

    # Remove from Stripe
    removed = stripe_service.remove_discount_from_subscription(
        subscription.stripe_subscription_id
    )
    if not removed:
        raise HTTPException(500, "Failed to remove discount from Stripe")

    # Remove from DB
    await discount_ops.remove_discount_for_org(db, request.organization_id)

    # Log billing event
    await subscription_ops.log_event(
        db,
        organization_id=request.organization_id,
        event_type=BillingEventType.DISCOUNT_REMOVED,
        description="Discount removed from subscription",
        actor_user_id=current_user.id,
    )

    await db.commit()

    logger.info(f"Removed discount from org {request.organization_id}")

    return {"message": "Discount removed"}


# ─────────────────────────────────────────────────────────────────────────────
# Webhook Handler
# ─────────────────────────────────────────────────────────────────────────────


@router.post("/webhooks/stripe")
async def handle_stripe_webhook(
    request: Request,
    db: DbSession,
) -> dict[str, str]:
    """
    Handle Stripe webhook events.

    This endpoint is called by Stripe when subscription events occur.
    Verifies the webhook signature before processing.
    No authentication required (verified by Stripe signature).
    """
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    # Verify signature
    try:
        event = stripe_service.construct_webhook_event(payload, sig_header)
    except ValueError:
        raise HTTPException(400, "Invalid webhook signature") from None

    event_type = str(event.get("type", ""))
    event_id = str(event.get("id", ""))
    data = event.get("data", {})
    obj = data.get("object", {}) if isinstance(data, dict) else {}

    logger.info(f"Received Stripe webhook: {event_type} ({event_id})")

    # Check for duplicate (idempotency)
    existing = await db.execute(
        select(BillingEvent).where(BillingEvent.stripe_event_id == event_id)  # type: ignore[arg-type]
    )
    if existing.scalar_one_or_none():
        logger.info(f"Skipping duplicate webhook: {event_id}")
        return {"status": "already_processed"}

    # Route to handler
    if event_type == "checkout.session.completed":
        await _handle_checkout_completed(db, obj, event_id)
    elif event_type == "customer.subscription.updated":
        await _handle_subscription_updated(db, obj, event_id)
    elif event_type == "customer.subscription.deleted":
        await _handle_subscription_deleted(db, obj, event_id)
    elif event_type == "invoice.payment_failed":
        await _handle_payment_failed(db, obj, event_id)
    elif event_type == "customer.subscription.trial_will_end":
        await _handle_trial_ending(db, obj, event_id)
    else:
        logger.debug(f"Unhandled webhook event type: {event_type}")

    await db.commit()
    return {"status": "ok"}


# ─────────────────────────────────────────────────────────────────────────────
# Webhook Handlers
# ─────────────────────────────────────────────────────────────────────────────


async def _handle_checkout_completed(
    db: DbSession,
    session: dict[str, Any],
    event_id: str,
) -> None:
    """Handle successful checkout — activate subscription and process referrals."""
    customer_id = session.get("customer", "")
    stripe_subscription_id = session.get("subscription", "")

    # Get plan tier from metadata
    metadata = session.get("metadata", {})
    plan_tier = metadata.get("plan_tier", "indie") if isinstance(metadata, dict) else "indie"

    subscription = await subscription_ops.get_by_stripe_customer(db, customer_id)
    if not subscription:
        logger.error(f"No subscription found for customer {customer_id}")
        return

    plan = get_plan(plan_tier)
    previous_tier = subscription.plan_tier

    # Determine status (trialing if subscription has trial)
    stripe_sub = stripe_service.get_subscription(stripe_subscription_id)
    status = SubscriptionStatus.ACTIVE.value
    if stripe_sub and stripe_sub.get("status") == "trialing":
        status = SubscriptionStatus.TRIALING.value

    # Build update dict — stamp first_subscribed_at on first-ever checkout
    updates: dict[str, Any] = {
        "plan_tier": plan_tier,
        "status": status,
        "base_repo_limit": plan.base_repo_limit,
        "stripe_subscription_id": stripe_subscription_id,
    }
    if subscription.first_subscribed_at is None:
        updates["first_subscribed_at"] = datetime.now(UTC)

    # Update subscription
    await subscription_ops.update(db, subscription, updates)

    # Log billing event
    await subscription_ops.log_event(
        db,
        organization_id=subscription.organization_id,
        event_type=BillingEventType.PLAN_CHANGED,
        previous_value={"plan_tier": previous_tier},
        new_value={"plan_tier": plan_tier, "status": status},
        stripe_event_id=event_id,
        description=f"Subscribed to {plan.display_name} via Stripe Checkout",
    )

    logger.info(f"Activated {plan_tier} subscription for org {subscription.organization_id}")

    # ─────────────────────────────────────────────────────────────────────────────
    # Discount Code Processing: Record redemption if a code was used at checkout
    # ─────────────────────────────────────────────────────────────────────────────
    discount_code = metadata.get("discount_code") if isinstance(metadata, dict) else None
    if discount_code:
        await _process_checkout_discount(db, discount_code, subscription, event_id)

    # ─────────────────────────────────────────────────────────────────────────────
    # Referral Processing: Check if this user signed up via referral
    # ─────────────────────────────────────────────────────────────────────────────
    await _process_referral_conversion(db, subscription, stripe_subscription_id, event_id)


async def _process_checkout_discount(
    db: DbSession,
    discount_code: str,
    subscription: Any,
    event_id: str,
) -> None:
    """
    Record a discount code redemption after checkout completes.

    Called from the checkout.session.completed webhook when the session
    metadata contains a discount_code (set during Phase 1 checkout creation).
    The coupon is already applied to the Stripe subscription at this point —
    this function records the redemption in our DB for tracking.
    """
    org = await organization_ops.get(db, subscription.organization_id)
    if not org:
        logger.error(f"No org found for discount redemption: {subscription.organization_id}")
        return

    # Validate first to get the DiscountCode object (needed for percent + coupon_id)
    try:
        discount = await discount_ops.validate_code(db, discount_code)
    except ValueError as e:
        logger.warning(f"Discount code {discount_code} no longer valid at webhook time: {e}")
        return

    try:
        await discount_ops.redeem_code(
            db, discount_code, subscription.organization_id, org.owner_id
        )
    except ValueError as e:
        # Code may have been exhausted between checkout creation and completion,
        # or org may already have a discount — the Stripe coupon is already applied
        # on the subscription, so log but don't fail the webhook.
        logger.warning(
            f"Discount redemption failed for code {discount_code} "
            f"(org {subscription.organization_id}): {e}"
        )
        return

    # Ensure stripe_coupon_id is persisted on the DiscountCode
    if not discount.stripe_coupon_id:
        discount.stripe_coupon_id = f"DISCOUNT_{discount_code.upper()}"
        db.add(discount)

    # Log billing event
    await subscription_ops.log_event(
        db,
        organization_id=subscription.organization_id,
        event_type=BillingEventType.DISCOUNT_APPLIED,
        new_value={
            "code": discount_code,
            "discount_percent": discount.discount_percent,
            "source": "checkout",
        },
        stripe_event_id=event_id,
        description=f"Discount code {discount_code} applied at checkout",
    )

    logger.info(
        f"Recorded discount redemption for code {discount_code} "
        f"(org {subscription.organization_id})"
    )


async def _process_referral_conversion(
    db: DbSession,
    recipient_subscription: Any,
    recipient_stripe_sub_id: str,
    event_id: str,
) -> None:
    """
    Process referral conversion when a referred user completes payment.

    Steps:
    1. Get the organization owner (recipient)
    2. Check if they have a pending referral
    3. Mark the referral as converted
    4. Apply 1 free month to recipient's subscription
    5. Apply 1 free month to sender's subscription (if they have one)
    """
    # Get the organization to find the owner (recipient)
    org = await organization_ops.get(db, recipient_subscription.organization_id)
    if not org:
        return

    recipient_user_id = org.owner_id

    # Check for pending referral
    referral = await referral_ops.get_pending_referral_for_recipient(db, recipient_user_id)
    if not referral:
        logger.debug(f"No pending referral for user {recipient_user_id}")
        return

    # Mark referral as converted
    await referral_ops.mark_converted(db, recipient_user_id)

    logger.info(
        f"Referral converted: code={referral.code}, "
        f"sender={referral.user_id}, recipient={recipient_user_id}"
    )

    # Apply 1 free month to recipient's subscription
    if recipient_stripe_sub_id:
        recipient_reward_applied = stripe_service.apply_referral_reward(recipient_stripe_sub_id)
        if recipient_reward_applied:
            await subscription_ops.log_event(
                db,
                organization_id=recipient_subscription.organization_id,
                event_type=BillingEventType.REFERRAL_CREDIT_APPLIED,
                new_value={
                    "type": "recipient",
                    "referral_code": referral.code,
                    "months_free": 1,
                },
                stripe_event_id=event_id,
                description="Referral reward: 1 free month applied (recipient)",
            )
            logger.info(f"Applied 1 free month to recipient subscription {recipient_stripe_sub_id}")

    # Apply 1 free month to sender's subscription (if they have one)
    await _apply_sender_referral_reward(db, referral, event_id)


async def _apply_sender_referral_reward(
    db: DbSession,
    referral: Any,
    event_id: str,
) -> None:
    """
    Apply referral reward to the sender (person who shared the code).

    If sender has an active Stripe subscription, apply 1 free month coupon.
    If not, increment their referral_credit_cents for future application.
    """
    sender_user_id = referral.user_id

    # Find sender's organizations to check for subscriptions
    sender_orgs = await organization_ops.get_for_user(db, sender_user_id)

    sender_subscription = None
    for org in sender_orgs:
        sub = await subscription_ops.get_by_org(db, org.id)
        if sub and sub.stripe_subscription_id:
            sender_subscription = sub
            break

    if sender_subscription and sender_subscription.stripe_subscription_id:
        # Sender has an active Stripe subscription - apply coupon
        reward_applied = stripe_service.apply_referral_reward(
            sender_subscription.stripe_subscription_id
        )
        if reward_applied:
            await subscription_ops.log_event(
                db,
                organization_id=sender_subscription.organization_id,
                event_type=BillingEventType.REFERRAL_EARNED,
                new_value={
                    "type": "sender",
                    "referral_code": referral.code,
                    "months_free": 1,
                },
                stripe_event_id=event_id,
                description="Referral reward earned: 1 free month applied",
            )
            logger.info(
                f"Applied 1 free month to sender subscription "
                f"{sender_subscription.stripe_subscription_id} for referral {referral.code}"
            )
    else:
        # Sender doesn't have an active Stripe subscription
        # Store credit for later (when they subscribe)
        # For MVP, we just log this - credit tracking can be added in future
        logger.info(
            f"Sender {sender_user_id} earned referral credit but has no active subscription. "
            f"Credit will be applied when they subscribe."
        )
        # TODO: Implement pending credit tracking in subscription.referral_credit_cents
        # and apply during their checkout


async def _handle_subscription_updated(
    db: DbSession,
    stripe_sub: dict[str, Any],
    event_id: str,
) -> None:
    """Handle subscription updates (status changes, period changes)."""
    customer_id = stripe_sub.get("customer", "")
    subscription = await subscription_ops.get_by_stripe_customer(db, customer_id)
    if not subscription:
        logger.warning(f"No subscription found for customer {customer_id}")
        return

    # Update period dates
    updates: dict[str, Any] = {
        "cancel_at_period_end": stripe_sub.get("cancel_at_period_end", False),
    }

    # Parse period timestamps
    period_start = stripe_sub.get("current_period_start")
    period_end = stripe_sub.get("current_period_end")
    if period_start:
        updates["current_period_start"] = datetime.fromtimestamp(period_start, tz=UTC)
    if period_end:
        updates["current_period_end"] = datetime.fromtimestamp(period_end, tz=UTC)

    # Update status
    status_map = {
        "active": SubscriptionStatus.ACTIVE.value,
        "trialing": SubscriptionStatus.TRIALING.value,
        "past_due": SubscriptionStatus.PAST_DUE.value,
        "canceled": SubscriptionStatus.CANCELED.value,
        "unpaid": SubscriptionStatus.UNPAID.value,
    }
    stripe_status = stripe_sub.get("status", "")
    if stripe_status in status_map:
        updates["status"] = status_map[stripe_status]

    await subscription_ops.update(db, subscription, updates)

    # Log event
    await subscription_ops.log_event(
        db,
        organization_id=subscription.organization_id,
        event_type=BillingEventType.SUBSCRIPTION_UPDATED,
        new_value={
            "status": updates.get("status"),
            "cancel_at_period_end": updates["cancel_at_period_end"],
        },
        stripe_event_id=event_id,
    )


async def _handle_subscription_deleted(
    db: DbSession,
    stripe_sub: dict[str, Any],
    event_id: str,
) -> None:
    """
    Handle subscription deletion — delete the organization.

    This webhook fires when the subscription actually ends (after the billing period),
    not when the user clicks cancel. Since Trajan has no free tier, an ended subscription
    means the organization should be deleted along with all its data.
    """
    customer_id = stripe_sub.get("customer", "")
    subscription = await subscription_ops.get_by_stripe_customer(db, customer_id)
    if not subscription:
        logger.warning(f"No subscription found for deleted Stripe customer {customer_id}")
        return

    org_id = subscription.organization_id
    previous_tier = subscription.plan_tier

    # Log the event before deletion (for audit trail)
    await subscription_ops.log_event(
        db,
        organization_id=org_id,
        event_type=BillingEventType.SUBSCRIPTION_CANCELED,
        previous_value={"plan_tier": previous_tier},
        new_value={"status": "deleted", "action": "organization_deleted"},
        stripe_event_id=event_id,
        description="Subscription ended — organization deleted",
    )

    # Delete the organization (cascades to products, repos, docs, etc.)
    deleted = await organization_ops.delete(db, org_id)

    if deleted:
        logger.info(
            f"Deleted organization {org_id} after subscription ended "
            f"(previous tier: {previous_tier})"
        )
    else:
        logger.error(f"Failed to delete organization {org_id} after subscription ended")


async def _handle_payment_failed(
    db: DbSession,
    invoice: dict[str, Any],
    event_id: str,
) -> None:
    """Handle failed payment — mark as past due."""
    customer_id = invoice.get("customer", "")
    subscription = await subscription_ops.get_by_stripe_customer(db, customer_id)
    if not subscription:
        return

    await subscription_ops.update(
        db,
        subscription,
        {"status": SubscriptionStatus.PAST_DUE.value},
    )

    await subscription_ops.log_event(
        db,
        organization_id=subscription.organization_id,
        event_type=BillingEventType.PAYMENT_FAILED,
        new_value={"status": "past_due", "invoice_id": invoice.get("id")},
        stripe_event_id=event_id,
        description="Payment failed",
    )

    logger.warning(f"Payment failed for org {subscription.organization_id}")


async def _handle_trial_ending(
    db: DbSession,
    stripe_sub: dict[str, Any],
    event_id: str,
) -> None:
    """Handle trial ending notification (3 days before trial ends)."""
    customer_id = stripe_sub.get("customer", "")
    subscription = await subscription_ops.get_by_stripe_customer(db, customer_id)
    if not subscription:
        return

    # Log event for potential follow-up (email notifications, etc.)
    await subscription_ops.log_event(
        db,
        organization_id=subscription.organization_id,
        event_type=BillingEventType.SUBSCRIPTION_UPDATED,
        new_value={"trial_ending": True, "trial_end": stripe_sub.get("trial_end")},
        stripe_event_id=event_id,
        description="Trial ending in 3 days",
    )

    logger.info(f"Trial ending soon for org {subscription.organization_id}")
