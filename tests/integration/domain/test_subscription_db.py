"""DB integration tests for SubscriptionOperations.

Tests real SQL execution against PostgreSQL via the rollback fixture.
Covers: lookup methods, plan updates, admin assignment, repo limit checks,
and billing event audit trail.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.subscription_operations import subscription_ops
from app.models.billing import BillingEventType

# ─────────────────────────────────────────────────────────────────────────────
# Lookups
# ─────────────────────────────────────────────────────────────────────────────


class TestSubscriptionLookups:
    """Test subscription retrieval methods."""

    async def test_get_by_org(self, db_session: AsyncSession, test_org, test_subscription):
        """Can retrieve the subscription for an organization."""
        sub = await subscription_ops.get_by_org(db_session, test_org.id)
        assert sub is not None
        assert sub.id == test_subscription.id
        assert sub.organization_id == test_org.id

    async def test_get_by_id(self, db_session: AsyncSession, test_subscription):
        """Can retrieve a subscription by its primary key."""
        sub = await subscription_ops.get(db_session, test_subscription.id)
        assert sub is not None
        assert sub.id == test_subscription.id

    async def test_get_by_stripe_customer(self, db_session: AsyncSession, test_subscription):
        """Can look up a subscription by Stripe customer ID."""
        # Set a stripe customer ID first
        await subscription_ops.update(
            db_session, test_subscription, {"stripe_customer_id": "cus_test_db_123"}
        )

        sub = await subscription_ops.get_by_stripe_customer(db_session, "cus_test_db_123")
        assert sub is not None
        assert sub.id == test_subscription.id

    async def test_get_by_stripe_customer_not_found(self, db_session: AsyncSession):
        """Returns None for a non-existent Stripe customer ID."""
        sub = await subscription_ops.get_by_stripe_customer(db_session, "cus_nonexistent")
        assert sub is None


# ─────────────────────────────────────────────────────────────────────────────
# Plan tier updates
# ─────────────────────────────────────────────────────────────────────────────


class TestPlanUpdates:
    """Test plan tier and status changes."""

    async def test_update_plan_tier(self, db_session: AsyncSession, test_subscription):
        """Can update the plan tier and see it reflected."""
        updated = await subscription_ops.update(
            db_session, test_subscription, {"plan_tier": "pro", "base_repo_limit": 10}
        )
        assert updated.plan_tier == "pro"
        assert updated.base_repo_limit == 10

    async def test_update_status(self, db_session: AsyncSession, test_subscription):
        """Can update subscription status."""
        updated = await subscription_ops.update(
            db_session, test_subscription, {"status": "past_due"}
        )
        assert updated.status == "past_due"

    async def test_admin_assign_plan(self, db_session: AsyncSession, test_user, test_subscription):
        """Admin can manually assign a plan, bypassing Stripe."""
        updated = await subscription_ops.admin_assign_plan(
            db_session,
            subscription=test_subscription,
            plan_tier="scale",
            admin_user_id=test_user.id,
            note="Testing admin assignment",
        )

        assert updated.plan_tier == "scale"
        assert updated.base_repo_limit == 50  # Scale plan limit
        assert updated.is_manually_assigned is True
        assert updated.manually_assigned_by == test_user.id
        assert updated.manual_assignment_note == "Testing admin assignment"
        assert updated.status == "active"


# ─────────────────────────────────────────────────────────────────────────────
# Repository limit checks
# ─────────────────────────────────────────────────────────────────────────────


class TestRepoLimits:
    """Test repository limit checking logic with real DB lookups."""

    async def test_under_limit(self, db_session: AsyncSession, test_org, test_subscription):
        """Indie plan (5 repos): 2 repos → can_add=True, no overage."""
        status = await subscription_ops.check_repo_limit(
            db_session, test_org.id, current_repo_count=2
        )
        assert status.can_add is True
        assert status.current_count == 2
        assert status.base_limit == 5
        assert status.overage_count == 0
        assert status.overage_cost_cents == 0

    async def test_at_limit(self, db_session: AsyncSession, test_org, test_subscription):
        """Indie plan (5 repos): 5 repos → can_add=False (hard cap)."""
        status = await subscription_ops.check_repo_limit(
            db_session, test_org.id, current_repo_count=5
        )
        assert status.can_add is False
        assert status.base_limit == 5

    async def test_over_limit_paid_plan(
        self, db_session: AsyncSession, test_org, test_subscription
    ):
        """Pro plan allows overages — can always add with overage charges."""
        # Upgrade to pro
        await subscription_ops.update(
            db_session, test_subscription, {"plan_tier": "pro", "base_repo_limit": 10}
        )

        status = await subscription_ops.check_repo_limit(
            db_session, test_org.id, current_repo_count=12
        )
        assert status.can_add is True
        assert status.allows_overages is True
        assert status.overage_count == 3  # 12 + 1 - 10 = 3
        assert status.overage_cost_cents == 3000  # 3 × $10


# ─────────────────────────────────────────────────────────────────────────────
# Billing event audit trail
# ─────────────────────────────────────────────────────────────────────────────


class TestBillingEvents:
    """Test billing event logging and retrieval."""

    async def test_admin_assign_logs_event(
        self, db_session: AsyncSession, test_user, test_org, test_subscription
    ):
        """admin_assign_plan logs a MANUAL_ASSIGNMENT billing event."""
        await subscription_ops.admin_assign_plan(
            db_session,
            subscription=test_subscription,
            plan_tier="pro",
            admin_user_id=test_user.id,
            note="Audit trail test",
        )

        events = await subscription_ops.get_events(db_session, test_org.id)
        manual_events = [
            e for e in events if e.event_type == BillingEventType.MANUAL_ASSIGNMENT.value
        ]
        assert len(manual_events) >= 1

        latest = manual_events[0]
        assert latest.actor_user_id == test_user.id
        assert latest.new_value["plan_tier"] == "pro"
        assert latest.new_value["note"] == "Audit trail test"

    async def test_user_event_round_trip(
        self, db_session: AsyncSession, test_user, test_org, test_subscription
    ):
        """Can log a user-attributed event and retrieve it (actor stamped, no stripe_event_id)."""
        await subscription_ops.log_user_event(
            db_session,
            organization_id=test_org.id,
            event_type=BillingEventType.PAYMENT_SUCCEEDED,
            actor_user_id=test_user.id,
            new_value={"amount_cents": 4900},
            description="Monthly payment",
        )

        events = await subscription_ops.get_events(db_session, test_org.id)
        payment_events = [
            e for e in events if e.event_type == BillingEventType.PAYMENT_SUCCEEDED.value
        ]
        assert len(payment_events) >= 1

        evt = payment_events[0]
        assert evt.new_value["amount_cents"] == 4900
        assert evt.description == "Monthly payment"
        assert evt.actor_user_id == test_user.id
        assert evt.stripe_event_id is None

    async def test_system_event_round_trip(
        self, db_session: AsyncSession, test_org, test_subscription
    ):
        """Can log a system-attributed event and retrieve it (stripe_event_id stamped, no actor)."""
        await subscription_ops.log_system_event(
            db_session,
            organization_id=test_org.id,
            event_type=BillingEventType.PAYMENT_SUCCEEDED,
            stripe_event_id="evt_test_system_123",
            new_value={"amount_cents": 4900},
            description="Webhook payment",
        )

        events = await subscription_ops.get_events(db_session, test_org.id)
        webhook_events = [
            e
            for e in events
            if e.event_type == BillingEventType.PAYMENT_SUCCEEDED.value
            and e.stripe_event_id == "evt_test_system_123"
        ]
        assert len(webhook_events) == 1

        evt = webhook_events[0]
        assert evt.new_value["amount_cents"] == 4900
        assert evt.description == "Webhook payment"
        assert evt.actor_user_id is None
        assert evt.stripe_event_id == "evt_test_system_123"
