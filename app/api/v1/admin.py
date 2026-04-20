"""Admin API endpoints for system administration."""

import logging
import uuid as uuid_pkg
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_system_admin
from app.config.plans import PLANS
from app.core.database import cron_session_maker, get_db
from app.domain import organization_ops, subscription_ops
from app.models.user import User

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


class AdminSubscriptionUpdate(BaseModel):
    """Schema for admin subscription update."""

    plan_tier: str
    note: str | None = None


class SubscriptionResponse(BaseModel):
    """Response schema for subscription data."""

    id: str
    organization_id: str
    organization_name: str
    plan_tier: str
    status: str
    base_repo_limit: int
    is_manually_assigned: bool
    manual_assignment_note: str | None
    stripe_customer_id: str | None
    created_at: str


class OrganizationSummary(BaseModel):
    """Summary of an organization for admin view."""

    id: str
    name: str
    slug: str
    owner_email: str | None
    plan_tier: str
    member_count: int
    created_at: str


class PlanInfo(BaseModel):
    """Plan information for API response."""

    tier: str
    display_name: str
    price_monthly: int
    base_repo_limit: int
    overage_repo_price: int
    allows_overages: bool
    features: dict[str, bool]


@router.get("/organizations")
async def list_organizations(
    skip: int = 0,
    limit: int = 50,
    _admin: User = Depends(require_system_admin),
    db: AsyncSession = Depends(get_db),
) -> list[OrganizationSummary]:
    """
    List all organizations (admin only).

    Returns a paginated list of all organizations with their subscription status.
    """
    orgs = await organization_ops.get_all(db, skip=skip, limit=limit)

    result = []
    for org in orgs:
        # Get subscription
        subscription = await subscription_ops.get_by_org(db, org.id)
        plan_tier = subscription.plan_tier if subscription else "observer"

        # Get member count
        org_with_members = await organization_ops.get_with_members(db, org.id)
        member_count = len(org_with_members.members) if org_with_members else 0

        # Get owner email
        owner_email = None
        if org.owner:
            owner_email = org.owner.email

        result.append(
            OrganizationSummary(
                id=str(org.id),
                name=org.name,
                slug=org.slug,
                owner_email=owner_email,
                plan_tier=plan_tier,
                member_count=member_count,
                created_at=org.created_at.isoformat(),
            )
        )

    return result


@router.get("/organizations/{org_id}")
async def get_organization(
    org_id: uuid_pkg.UUID,
    _admin: User = Depends(require_system_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Get detailed organization info (admin only).

    Returns full organization data including subscription and members.
    """
    org = await organization_ops.get_with_members(db, org_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    subscription = await subscription_ops.get_by_org(db, org_id)

    return {
        "id": str(org.id),
        "name": org.name,
        "slug": org.slug,
        "owner_id": str(org.owner_id),
        "created_at": org.created_at.isoformat(),
        "members": [
            {
                "user_id": str(m.user_id),
                "role": m.role,
                "joined_at": m.joined_at.isoformat(),
            }
            for m in org.members
        ],
        "subscription": (
            {
                "id": str(subscription.id),
                "plan_tier": subscription.plan_tier,
                "status": subscription.status,
                "base_repo_limit": subscription.base_repo_limit,
                "is_manually_assigned": subscription.is_manually_assigned,
                "manual_assignment_note": subscription.manual_assignment_note,
                "stripe_customer_id": subscription.stripe_customer_id,
            }
            if subscription
            else None
        ),
    }


@router.get("/organizations/{org_id}/subscription")
async def get_organization_subscription(
    org_id: uuid_pkg.UUID,
    _admin: User = Depends(require_system_admin),
    db: AsyncSession = Depends(get_db),
) -> SubscriptionResponse:
    """
    Get subscription details for an organization (admin only).
    """
    org = await organization_ops.get(db, org_id)
    if not org:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )

    subscription = await subscription_ops.get_by_org(db, org_id)
    if not subscription:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Subscription not found",
        )

    return SubscriptionResponse(
        id=str(subscription.id),
        organization_id=str(subscription.organization_id),
        organization_name=org.name,
        plan_tier=subscription.plan_tier,
        status=subscription.status,
        base_repo_limit=subscription.base_repo_limit,
        is_manually_assigned=subscription.is_manually_assigned,
        manual_assignment_note=subscription.manual_assignment_note,
        stripe_customer_id=subscription.stripe_customer_id,
        created_at=subscription.created_at.isoformat(),
    )


@router.patch("/organizations/{org_id}/subscription")
async def admin_set_subscription(
    org_id: uuid_pkg.UUID,
    data: AdminSubscriptionUpdate,
    admin: User = Depends(require_system_admin),
) -> SubscriptionResponse:
    """
    Manually assign a plan tier to an organization (admin only).

    This bypasses Stripe and sets the plan directly. Use cases:
    - Founder account
    - Beta testers
    - Manual trials
    - Enterprise deals

    Runs on ``cron_session_maker`` (BYPASSRLS). A system admin is not
    a member of the target org, so the ``billing_events_member_insert``
    policy would reject the ``MANUAL_ASSIGNMENT`` event on the regular
    ``trajan_app`` connection. Platform-scoped break-glass writes go
    through the cron role; ``admin_user_id=admin.id`` is still stamped
    as ``actor_user_id`` for the audit trail.
    """
    # Validate plan tier
    if data.plan_tier not in PLANS:
        valid_tiers = list(PLANS.keys())
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid plan tier. Must be one of: {valid_tiers}",
        )

    async with cron_session_maker() as db:
        org = await organization_ops.get(db, org_id)
        if not org:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Organization not found",
            )

        subscription = await subscription_ops.get_by_org(db, org_id)
        if not subscription:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Subscription not found",
            )

        updated = await subscription_ops.admin_assign_plan(
            db,
            subscription=subscription,
            plan_tier=data.plan_tier,
            admin_user_id=admin.id,
            note=data.note,
        )

        await db.commit()

        logger.info(
            f"Admin {admin.id} assigned plan {data.plan_tier} to org {org_id} "
            f"(cron_session_maker / BYPASSRLS)"
        )

        return SubscriptionResponse(
            id=str(updated.id),
            organization_id=str(updated.organization_id),
            organization_name=org.name,
            plan_tier=updated.plan_tier,
            status=updated.status,
            base_repo_limit=updated.base_repo_limit,
            is_manually_assigned=updated.is_manually_assigned,
            manual_assignment_note=updated.manual_assignment_note,
            stripe_customer_id=updated.stripe_customer_id,
            created_at=updated.created_at.isoformat(),
        )


@router.get("/plans")
async def list_plans(
    _admin: User = Depends(require_system_admin),
) -> list[PlanInfo]:
    """
    List all available plan tiers (admin only).

    Returns configuration details for all plans.
    """
    return [
        PlanInfo(
            tier=plan.tier,
            display_name=plan.display_name,
            price_monthly=plan.price_monthly,
            base_repo_limit=plan.base_repo_limit,
            overage_repo_price=plan.overage_repo_price,
            allows_overages=plan.allows_overages,
            features=plan.features,
        )
        for plan in PLANS.values()
    ]
