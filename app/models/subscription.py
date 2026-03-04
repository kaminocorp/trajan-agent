"""Subscription model - organization billing and plan management."""

import uuid as uuid_pkg
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Optional

from sqlalchemy import Column, DateTime, ForeignKey, String, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlmodel import Field, Relationship, SQLModel

if TYPE_CHECKING:
    from app.models.organization import Organization
    from app.models.user import User


class PlanTier(str, Enum):
    """Available subscription plan tiers."""

    # Pending state (no plan selected yet)
    NONE = "none"  # New signup, awaiting plan selection

    # New tier names (Indie/Pro/Scale pricing)
    INDIE = "indie"  # $49/mo - 5 repos
    PRO = "pro"  # $299/mo - 10 repos
    SCALE = "scale"  # $499/mo - 50 repos

    # Legacy tier names (for backwards compatibility with existing data)
    OBSERVER = "observer"  # Free - $0 (no longer offered, but may exist in DB)
    FOUNDATIONS = "foundations"  # Legacy: renamed to Indie
    CORE = "core"  # Legacy: renamed to Pro
    AUTONOMOUS = "autonomous"  # Legacy: renamed to Scale


class SubscriptionStatus(str, Enum):
    """Subscription lifecycle states."""

    PENDING = "pending"  # Awaiting plan selection (new signups)
    ACTIVE = "active"
    PAST_DUE = "past_due"
    CANCELED = "canceled"
    UNPAID = "unpaid"
    TRIALING = "trialing"  # 14-day free trial


class Subscription(SQLModel, table=True):
    """
    Subscription model - tracks an organization's billing status.

    Each organization has exactly one subscription. Free tier organizations
    get a subscription with plan_tier=OBSERVER.

    Supports two assignment modes:
    1. Stripe-managed: Payment through Stripe, subscription synced via webhooks
    2. Manually assigned: Admin sets plan directly (for founders, beta testers)
    """

    __tablename__ = "subscriptions"

    id: uuid_pkg.UUID = Field(
        default_factory=uuid_pkg.uuid4,
        primary_key=True,
        nullable=False,
        sa_column_kwargs={"server_default": text("gen_random_uuid()")},
    )
    organization_id: uuid_pkg.UUID = Field(
        sa_column=Column(
            PG_UUID(as_uuid=True),
            ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
    )

    # Plan info
    plan_tier: str = Field(
        default=PlanTier.OBSERVER.value,
        sa_column=Column(
            String(20),
            nullable=False,
            server_default=PlanTier.OBSERVER.value,
        ),
    )
    status: str = Field(
        default=SubscriptionStatus.ACTIVE.value,
        sa_column=Column(
            String(20),
            nullable=False,
            server_default=SubscriptionStatus.ACTIVE.value,
        ),
    )

    # Repository limits (base from plan, metered billing for overages on paid plans)
    base_repo_limit: int = Field(
        default=1,
        nullable=False,
        sa_column_kwargs={"server_default": text("1")},
    )

    # Billing period
    current_period_start: datetime | None = Field(  # type: ignore[call-overload]
        default=None, nullable=True, sa_type=DateTime(timezone=True)
    )
    current_period_end: datetime | None = Field(  # type: ignore[call-overload]
        default=None, nullable=True, sa_type=DateTime(timezone=True)
    )
    cancel_at_period_end: bool = Field(
        default=False,
        nullable=False,
        sa_column_kwargs={"server_default": text("false")},
    )
    canceled_at: datetime | None = Field(  # type: ignore[call-overload]
        default=None, nullable=True, sa_type=DateTime(timezone=True)
    )

    # Stripe references (nullable for manually assigned subscriptions)
    stripe_customer_id: str | None = Field(default=None, max_length=255, nullable=True, index=True)
    stripe_subscription_id: str | None = Field(default=None, max_length=255, nullable=True)
    stripe_metered_item_id: str | None = Field(
        default=None,
        max_length=255,
        nullable=True,
        sa_column_kwargs={"comment": "Stripe subscription item ID for overage billing"},
    )

    # Trial tracking
    first_subscribed_at: datetime | None = Field(  # type: ignore[call-overload]
        default=None,
        nullable=True,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={
            "comment": "When this org first subscribed — used to determine trial eligibility",
        },
    )

    # Referral tracking
    referral_credit_cents: int = Field(
        default=0,
        nullable=False,
        sa_column_kwargs={
            "server_default": text("0"),
            "comment": "Accumulated referral credits in cents",
        },
    )

    # Admin override fields - for manual plan assignment without Stripe
    is_manually_assigned: bool = Field(
        default=False,
        nullable=False,
        sa_column_kwargs={
            "server_default": text("false"),
            "comment": "True if plan was assigned by admin, bypassing Stripe",
        },
    )
    manually_assigned_by: uuid_pkg.UUID | None = Field(
        default=None,
        sa_column=Column(
            PG_UUID(as_uuid=True),
            ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    manually_assigned_at: datetime | None = Field(  # type: ignore[call-overload]
        default=None, nullable=True, sa_type=DateTime(timezone=True)
    )
    manual_assignment_note: str | None = Field(
        default=None,
        max_length=500,
        nullable=True,
        sa_column_kwargs={"comment": "Reason for manual assignment, e.g., 'Founder account'"},
    )
    manual_assignment_expires_at: datetime | None = Field(  # type: ignore[call-overload]
        default=None,
        nullable=True,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={
            "comment": "When manual assignment expires — org reverts to no-plan after this date",
        },
    )

    # Timestamps
    created_at: datetime = Field(  # type: ignore[call-overload]
        default_factory=lambda: datetime.now(UTC),
        nullable=False,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": text("now()")},
    )
    updated_at: datetime | None = Field(  # type: ignore[call-overload]
        default=None,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"onupdate": text("now()")},
    )

    # Relationships
    organization: Optional["Organization"] = Relationship(back_populates="subscription")
    assigned_by: Optional["User"] = Relationship(
        sa_relationship_kwargs={"foreign_keys": "[Subscription.manually_assigned_by]"}
    )


# Request/Response schemas
class SubscriptionUpdate(SQLModel):
    """Schema for admin subscription update."""

    plan_tier: str
    note: str | None = None
