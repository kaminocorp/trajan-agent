"""Stripe payment service for subscription management."""

import logging
from datetime import UTC, datetime

import stripe
from stripe import StripeError

from app.config import settings
from app.config.plans import get_plan
from app.models.organization import Organization
from app.models.subscription import Subscription
from app.models.user import User

logger = logging.getLogger(__name__)

# Initialize Stripe with secret key
stripe.api_key = settings.stripe_secret_key

# Trial period in days
TRIAL_PERIOD_DAYS = 14


class StripeService:
    """
    Handles all Stripe API interactions.

    All methods are static and stateless. Stripe SDK handles connection pooling.

    Pricing model:
    - Indie: $49/mo, 5 repos included
    - Pro: $299/mo, 10 repos included
    - Scale: $499/mo, 50 repos included
    - Overage: $10/repo/mo (flat rate for all tiers)
    """

    @staticmethod
    def get_price_id(plan_tier: str) -> str:
        """Get base price ID for a plan tier."""
        price_map = {
            "indie": settings.stripe_price_indie_base,
            "pro": settings.stripe_price_pro_base,
            "scale": settings.stripe_price_scale_base,
        }
        return price_map.get(plan_tier, "")

    @staticmethod
    def create_customer(org: Organization, user: User) -> str:
        """
        Create a Stripe customer for an organization.

        Returns the Stripe customer ID (cus_...).
        """
        try:
            customer = stripe.Customer.create(
                email=user.email or "",
                name=org.name,
                metadata={
                    "organization_id": str(org.id),
                    "owner_user_id": str(user.id),
                    "environment": "production" if "live" in (settings.stripe_secret_key or "") else "test",
                },
            )
            logger.info(f"Created Stripe customer {customer.id} for org {org.id}")
            return customer.id
        except StripeError as e:
            logger.error(f"Failed to create Stripe customer: {e}")
            raise

    @staticmethod
    def create_checkout_session(
        customer_id: str,
        plan_tier: str,
        success_url: str,
        cancel_url: str,
        include_trial: bool = True,
        coupon_id: str | None = None,
        discount_code: str | None = None,
    ) -> str:
        """
        Create a Stripe Checkout session for plan subscription.

        Returns the checkout session URL.
        Includes a 14-day free trial only if include_trial is True
        (first-time subscribers only).

        If coupon_id is provided, the discount is applied to the Checkout Session
        so Stripe displays the discounted price during payment. The discount_code
        is stored in session metadata so the webhook can record the redemption.
        """
        base_price = StripeService.get_price_id(plan_tier)

        if not base_price:
            raise ValueError(f"No base price configured for tier: {plan_tier}")

        line_items = [{"price": base_price, "quantity": 1}]

        # Add metered overage price if configured
        overage_price = settings.stripe_price_repo_overage
        if overage_price:
            line_items.append({"price": overage_price})  # Metered, no quantity

        subscription_data: dict[str, object] = {"metadata": {"plan_tier": plan_tier}}
        if include_trial:
            subscription_data["trial_period_days"] = TRIAL_PERIOD_DAYS

        # Session metadata — used by webhook to record discount redemption
        session_metadata: dict[str, str] = {"plan_tier": plan_tier}
        if discount_code:
            session_metadata["discount_code"] = discount_code

        # Build session kwargs
        session_kwargs: dict[str, object] = {
            "customer": customer_id,
            "mode": "subscription",
            "line_items": line_items,
            "success_url": success_url,
            "cancel_url": cancel_url,
            "subscription_data": subscription_data,
            "metadata": session_metadata,
        }

        if coupon_id:
            session_kwargs["discounts"] = [{"coupon": coupon_id}]

        try:
            session = stripe.checkout.Session.create(**session_kwargs)  # type: ignore[arg-type]
            logger.info(
                f"Created checkout session for customer {customer_id}, "
                f"tier {plan_tier}, trial={include_trial}, discount={discount_code}"
            )
            return session.url or ""
        except StripeError as e:
            logger.error(f"Failed to create checkout session: {e}")
            raise

    @staticmethod
    def create_portal_session(customer_id: str, return_url: str) -> str:
        """
        Create a Stripe Customer Portal session for self-service billing.

        Returns the portal session URL.
        """
        try:
            session = stripe.billing_portal.Session.create(
                customer=customer_id,
                return_url=return_url,
            )
            return session.url
        except StripeError as e:
            logger.error(f"Failed to create portal session: {e}")
            raise

    @staticmethod
    def report_repo_usage(
        subscription: Subscription,
        repo_count: int,
    ) -> None:
        """
        Report repository usage to Stripe for metered billing.

        Only reports overage (repos above base limit).
        Uses the meter event API with 'Last' aggregation.
        """
        if not subscription.stripe_customer_id:
            return  # No Stripe customer

        if not settings.stripe_meter_id:
            logger.debug("No meter ID configured, skipping usage report")
            return

        plan = get_plan(subscription.plan_tier)
        overage_count = max(0, repo_count - plan.base_repo_limit)

        try:
            # Create a meter event to report current overage
            stripe.billing.MeterEvent.create(
                event_name="repository_overage",
                payload={
                    "stripe_customer_id": subscription.stripe_customer_id,
                    "value": str(overage_count),
                },
                timestamp=int(datetime.now(UTC).timestamp()),
            )
            logger.info(
                f"Reported repo usage for subscription {subscription.id}: "
                f"{repo_count} total, {overage_count} overage"
            )
        except StripeError as e:
            logger.error(f"Failed to report usage: {e}")
            # Don't raise - usage reporting failure shouldn't break the app

    @staticmethod
    def cancel_subscription(stripe_subscription_id: str) -> None:
        """Cancel a Stripe subscription at period end."""
        try:
            stripe.Subscription.modify(
                stripe_subscription_id,
                cancel_at_period_end=True,
            )
            logger.info(f"Marked subscription {stripe_subscription_id} for cancellation")
        except StripeError as e:
            logger.error(f"Failed to cancel subscription: {e}")
            raise

    @staticmethod
    def reactivate_subscription(stripe_subscription_id: str) -> None:
        """Reactivate a subscription that was set to cancel at period end."""
        try:
            stripe.Subscription.modify(
                stripe_subscription_id,
                cancel_at_period_end=False,
            )
            logger.info(f"Reactivated subscription {stripe_subscription_id}")
        except StripeError as e:
            logger.error(f"Failed to reactivate subscription: {e}")
            raise

    @staticmethod
    def change_subscription_plan(stripe_subscription_id: str, new_plan_tier: str) -> None:
        """
        Change a subscription to a different plan tier.

        Prorates the change based on remaining time in current period.
        """
        new_price = StripeService.get_price_id(new_plan_tier)
        if not new_price:
            raise ValueError(f"No price configured for tier: {new_plan_tier}")

        try:
            # Get current subscription to find the subscription item
            sub = stripe.Subscription.retrieve(stripe_subscription_id)
            # Find the base price item (not the metered overage item)
            base_item = None
            for item in sub["items"]["data"]:
                # Metered items don't have a quantity, base items have quantity=1
                if item.get("quantity") == 1:
                    base_item = item
                    break

            if not base_item:
                raise ValueError("Could not find base subscription item")

            # Update to new price
            stripe.Subscription.modify(
                stripe_subscription_id,
                items=[
                    {
                        "id": base_item["id"],
                        "price": new_price,
                    }
                ],
                proration_behavior="create_prorations",
                metadata={"plan_tier": new_plan_tier},
            )
            logger.info(f"Changed subscription {stripe_subscription_id} to {new_plan_tier}")
        except StripeError as e:
            logger.error(f"Failed to change subscription plan: {e}")
            raise

    @staticmethod
    def construct_webhook_event(payload: bytes, signature: str) -> dict[str, object]:
        """
        Verify and construct a webhook event from Stripe.

        Raises ValueError if signature verification fails.
        """
        try:
            event = stripe.Webhook.construct_event(  # type: ignore[no-untyped-call]
                payload,
                signature,
                settings.stripe_webhook_secret,
            )
            return dict(event)
        except stripe.error.SignatureVerificationError as e:
            logger.warning(f"Webhook signature verification failed: {e}")
            raise ValueError("Invalid webhook signature") from None
        except ValueError as e:
            logger.warning(f"Invalid webhook payload: {e}")
            raise ValueError("Invalid webhook payload") from None

    @staticmethod
    def get_subscription(stripe_subscription_id: str) -> dict[str, object] | None:
        """Retrieve a Stripe subscription by ID."""
        try:
            sub = stripe.Subscription.retrieve(stripe_subscription_id)
            return dict(sub)
        except StripeError as e:
            logger.error(f"Failed to retrieve subscription: {e}")
            return None

    # ─────────────────────────────────────────────────────────────────────────────
    # Referral Coupon Management
    # ─────────────────────────────────────────────────────────────────────────────

    @staticmethod
    def get_or_create_referral_coupon() -> str:
        """
        Get or create a referral coupon for 1 free month.

        Returns the coupon ID. Creates the coupon if it doesn't exist.
        """
        # Check if coupon ID is configured
        if settings.stripe_referral_coupon_id:
            try:
                # Verify coupon exists
                stripe.Coupon.retrieve(settings.stripe_referral_coupon_id)
                return settings.stripe_referral_coupon_id
            except StripeError:
                logger.warning(
                    f"Configured coupon {settings.stripe_referral_coupon_id} not found, creating new one"
                )

        # Create a new coupon: 100% off for 1 month
        try:
            coupon = stripe.Coupon.create(
                id="REFERRAL_1_MONTH_FREE",  # Idempotent ID
                percent_off=100,
                duration="once",  # Applies once (to one invoice)
                name="Referral Reward - 1 Month Free",
                metadata={
                    "type": "referral",
                    "description": "1 free month for referral program participants",
                },
            )
            logger.info(f"Created referral coupon: {coupon.id}")
            return coupon.id
        except StripeError as e:
            # If coupon already exists with that ID, retrieve it
            if "already exists" in str(e).lower():
                return "REFERRAL_1_MONTH_FREE"
            logger.error(f"Failed to create referral coupon: {e}")
            raise

    @staticmethod
    def apply_coupon_to_subscription(
        stripe_subscription_id: str,
        coupon_id: str,
    ) -> bool:
        """
        Apply a coupon to an existing subscription.

        Returns True if successful, False otherwise.
        Uses the discounts parameter which is the modern Stripe API.
        """
        try:
            stripe.Subscription.modify(
                stripe_subscription_id,
                discounts=[{"coupon": coupon_id}],
            )
            logger.info(f"Applied coupon {coupon_id} to subscription {stripe_subscription_id}")
            return True
        except StripeError as e:
            logger.error(f"Failed to apply coupon to subscription: {e}")
            return False

    @staticmethod
    def apply_referral_reward(stripe_subscription_id: str) -> bool:
        """
        Apply referral reward (1 free month) to a subscription.

        Convenience wrapper around get_or_create_referral_coupon + apply_coupon_to_subscription.
        """
        try:
            coupon_id = StripeService.get_or_create_referral_coupon()
            return StripeService.apply_coupon_to_subscription(stripe_subscription_id, coupon_id)
        except StripeError as e:
            logger.error(f"Failed to apply referral reward: {e}")
            return False

    # ─────────────────────────────────────────────────────────────────────────────
    # Discount Coupon Management
    # ─────────────────────────────────────────────────────────────────────────────

    @staticmethod
    def get_or_create_discount_coupon(code: str, percent_off: int) -> str:
        """
        Get or create a Stripe coupon for a discount code.

        Uses the code as part of the coupon ID for idempotency.
        Duration is "forever" so the discount recurs every billing cycle.

        Returns the Stripe coupon ID.
        """
        coupon_id = f"DISCOUNT_{code.upper()}"

        try:
            coupon = stripe.Coupon.retrieve(coupon_id)
            logger.info(f"Retrieved existing discount coupon: {coupon.id}")
            return coupon.id
        except StripeError:
            pass  # Doesn't exist yet, create it

        try:
            coupon = stripe.Coupon.create(
                id=coupon_id,
                percent_off=percent_off,
                duration="forever",
                name=f"Discount Code: {code.upper()} ({percent_off}% off)",
                metadata={
                    "type": "discount_code",
                    "code": code.upper(),
                },
            )
            logger.info(f"Created discount coupon: {coupon.id}")
            return coupon.id
        except StripeError as e:
            if "already exists" in str(e).lower():
                return coupon_id
            logger.error(f"Failed to create discount coupon: {e}")
            raise

    @staticmethod
    def apply_discount_to_subscription(
        stripe_subscription_id: str,
        coupon_id: str,
    ) -> bool:
        """
        Apply a discount coupon to a subscription.

        Replaces any existing discount on the subscription.
        Returns True if successful, False otherwise.
        """
        try:
            stripe.Subscription.modify(
                stripe_subscription_id,
                discounts=[{"coupon": coupon_id}],
            )
            logger.info(
                f"Applied discount coupon {coupon_id} to subscription "
                f"{stripe_subscription_id}"
            )
            return True
        except StripeError as e:
            logger.error(f"Failed to apply discount to subscription: {e}")
            return False

    @staticmethod
    def remove_discount_from_subscription(stripe_subscription_id: str) -> bool:
        """
        Remove all discounts from a subscription.

        Returns True if successful, False otherwise.
        """
        try:
            stripe.Subscription.modify(
                stripe_subscription_id,
                discounts=[],
            )
            logger.info(f"Removed discount from subscription {stripe_subscription_id}")
            return True
        except StripeError as e:
            logger.error(f"Failed to remove discount from subscription: {e}")
            return False

    @staticmethod
    def get_customer_subscriptions(stripe_customer_id: str) -> list[dict[str, object]]:
        """
        Get all subscriptions for a Stripe customer.

        Returns list of subscription dicts.
        """
        try:
            subscriptions = stripe.Subscription.list(
                customer=stripe_customer_id,
                status="all",
                limit=10,
            )
            return [dict(sub) for sub in subscriptions.data]
        except StripeError as e:
            logger.error(f"Failed to list customer subscriptions: {e}")
            return []


# Singleton instance
stripe_service = StripeService()
