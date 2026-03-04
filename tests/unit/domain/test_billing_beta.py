"""Unit tests for beta discount code billing endpoints.

Tests the /redeem-beta-code endpoint and the is_beta guards
on /checkout and /apply-discount.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api.v1.billing import RedeemBetaCodeRequest, redeem_beta_code
from tests.helpers.mock_factories import (
    make_mock_discount_code,
    make_mock_organization,
    make_mock_subscription,
    make_mock_user,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db() -> MagicMock:
    db = MagicMock()
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    db.add = MagicMock()
    return db


# ---------------------------------------------------------------------------
# POST /redeem-beta-code — happy path
# ---------------------------------------------------------------------------


class TestRedeemBetaCodeHappyPath:
    """Beta code redeems successfully and activates Pro."""

    def setup_method(self):
        self.db = _make_db()
        self.user = make_mock_user()
        self.org_id = uuid.uuid4()
        self.beta_code = make_mock_discount_code(
            code="BETA-2026", discount_percent=100, is_beta=True
        )
        self.subscription = make_mock_subscription(
            tier="none", status="pending", organization_id=self.org_id
        )

    @pytest.mark.asyncio
    @patch("app.api.v1.billing.subscription_ops")
    @patch("app.api.v1.billing.organization_ops")
    @patch("app.api.v1.billing.discount_ops")
    async def test_returns_pro_active(self, mock_discount, mock_org, mock_sub):
        mock_discount.validate_code = AsyncMock(return_value=self.beta_code)
        mock_org.get_member_role = AsyncMock(return_value="owner")
        mock_sub.get_by_org = AsyncMock(return_value=self.subscription)
        mock_sub.update = AsyncMock()
        mock_sub.log_event = AsyncMock()
        mock_discount.redeem_code = AsyncMock()

        request = RedeemBetaCodeRequest(code="BETA-2026", organization_id=self.org_id)
        result = await redeem_beta_code(request, self.db, self.user)

        assert result.plan_tier == "pro"
        assert result.status == "active"
        mock_sub.update.assert_called_once()
        mock_discount.redeem_code.assert_called_once()
        mock_sub.log_event.assert_called_once()
        self.db.commit.assert_called_once()

    @pytest.mark.asyncio
    @patch("app.api.v1.billing.subscription_ops")
    @patch("app.api.v1.billing.organization_ops")
    @patch("app.api.v1.billing.discount_ops")
    async def test_passes_validated_discount_to_redeem(self, mock_discount, mock_org, mock_sub):
        """Confirms Fix 4: validated_discount kwarg is passed to avoid double-validation."""
        mock_discount.validate_code = AsyncMock(return_value=self.beta_code)
        mock_org.get_member_role = AsyncMock(return_value="owner")
        mock_sub.get_by_org = AsyncMock(return_value=self.subscription)
        mock_sub.update = AsyncMock()
        mock_sub.log_event = AsyncMock()
        mock_discount.redeem_code = AsyncMock()

        request = RedeemBetaCodeRequest(code="BETA-2026", organization_id=self.org_id)
        await redeem_beta_code(request, self.db, self.user)

        call_kwargs = mock_discount.redeem_code.call_args
        assert call_kwargs.kwargs.get("validated_discount") is self.beta_code


# ---------------------------------------------------------------------------
# POST /redeem-beta-code — error cases
# ---------------------------------------------------------------------------


class TestRedeemBetaCodeErrors:
    """Error handling for the redeem-beta-code endpoint."""

    def setup_method(self):
        self.db = _make_db()
        self.user = make_mock_user()
        self.org_id = uuid.uuid4()

    @pytest.mark.asyncio
    @patch("app.api.v1.billing.discount_ops")
    async def test_rejects_non_beta_code(self, mock_discount):
        regular_code = make_mock_discount_code(code="PROMO-50", discount_percent=50, is_beta=False)
        mock_discount.validate_code = AsyncMock(return_value=regular_code)

        request = RedeemBetaCodeRequest(code="PROMO-50", organization_id=self.org_id)
        with pytest.raises(HTTPException) as exc_info:
            await redeem_beta_code(request, self.db, self.user)
        assert exc_info.value.status_code == 400
        assert "not a beta code" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    @patch("app.api.v1.billing.discount_ops")
    async def test_rejects_invalid_code(self, mock_discount):
        mock_discount.validate_code = AsyncMock(side_effect=ValueError("Invalid discount code"))

        request = RedeemBetaCodeRequest(code="FAKE", organization_id=self.org_id)
        with pytest.raises(HTTPException) as exc_info:
            await redeem_beta_code(request, self.db, self.user)
        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    @patch("app.api.v1.billing.organization_ops")
    @patch("app.api.v1.billing.discount_ops")
    async def test_rejects_non_admin(self, mock_discount, mock_org):
        beta_code = make_mock_discount_code(code="BETA-2026", is_beta=True)
        mock_discount.validate_code = AsyncMock(return_value=beta_code)
        mock_org.get_member_role = AsyncMock(return_value="member")

        request = RedeemBetaCodeRequest(code="BETA-2026", organization_id=self.org_id)
        with pytest.raises(HTTPException) as exc_info:
            await redeem_beta_code(request, self.db, self.user)
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    @patch("app.api.v1.billing.subscription_ops")
    @patch("app.api.v1.billing.organization_ops")
    @patch("app.api.v1.billing.discount_ops")
    async def test_rejects_already_active_plan(self, mock_discount, mock_org, mock_sub):
        beta_code = make_mock_discount_code(code="BETA-2026", is_beta=True)
        mock_discount.validate_code = AsyncMock(return_value=beta_code)
        mock_org.get_member_role = AsyncMock(return_value="owner")
        mock_sub.get_by_org = AsyncMock(
            return_value=make_mock_subscription(tier="pro", status="active")
        )

        request = RedeemBetaCodeRequest(code="BETA-2026", organization_id=self.org_id)
        with pytest.raises(HTTPException) as exc_info:
            await redeem_beta_code(request, self.db, self.user)
        assert exc_info.value.status_code == 400
        assert "already has an active plan" in str(exc_info.value.detail)

    @pytest.mark.asyncio
    @patch("app.api.v1.billing.subscription_ops")
    @patch("app.api.v1.billing.organization_ops")
    @patch("app.api.v1.billing.discount_ops")
    async def test_rejects_missing_subscription(self, mock_discount, mock_org, mock_sub):
        beta_code = make_mock_discount_code(code="BETA-2026", is_beta=True)
        mock_discount.validate_code = AsyncMock(return_value=beta_code)
        mock_org.get_member_role = AsyncMock(return_value="owner")
        mock_sub.get_by_org = AsyncMock(return_value=None)

        request = RedeemBetaCodeRequest(code="BETA-2026", organization_id=self.org_id)
        with pytest.raises(HTTPException) as exc_info:
            await redeem_beta_code(request, self.db, self.user)
        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# Checkout beta guard (Fix 1)
# ---------------------------------------------------------------------------


class TestCheckoutBetaGuard:
    """Verify /checkout rejects beta codes passed as discount_code."""

    @pytest.mark.asyncio
    @patch("app.api.v1.billing.settings")
    @patch("app.api.v1.billing.subscription_ops")
    @patch("app.api.v1.billing.organization_ops")
    @patch("app.api.v1.billing.discount_ops")
    async def test_checkout_rejects_beta_discount_code(
        self, mock_discount, mock_org, mock_sub, mock_settings
    ):
        from app.api.v1.billing import CheckoutRequest, create_checkout

        mock_settings.stripe_enabled = True
        mock_settings.frontend_url = "http://localhost:3000"

        beta_code = make_mock_discount_code(code="BETA-2026", discount_percent=100, is_beta=True)
        mock_discount.validate_code = AsyncMock(return_value=beta_code)
        mock_org.get_member_role = AsyncMock(return_value="owner")
        mock_org.get = AsyncMock(return_value=make_mock_organization())
        mock_sub.get_by_org = AsyncMock(
            return_value=make_mock_subscription(
                tier="none",
                status="pending",
                stripe_customer_id="cus_123",
                first_subscribed_at=None,
            )
        )

        db = _make_db()
        user = make_mock_user()
        request = CheckoutRequest(
            organization_id=uuid.uuid4(),
            plan_tier="pro",
            discount_code="BETA-2026",
        )
        with pytest.raises(HTTPException) as exc_info:
            await create_checkout(request, db, user)
        assert exc_info.value.status_code == 400
        assert "Beta codes cannot be used at checkout" in str(exc_info.value.detail)


# ---------------------------------------------------------------------------
# Apply-discount beta guard (Fix 2)
# ---------------------------------------------------------------------------


class TestApplyDiscountBetaGuard:
    """Verify /apply-discount rejects beta codes."""

    @pytest.mark.asyncio
    @patch("app.api.v1.billing.subscription_ops")
    @patch("app.api.v1.billing.organization_ops")
    @patch("app.api.v1.billing.discount_ops")
    async def test_apply_discount_rejects_beta_code(self, mock_discount, mock_org, mock_sub):
        from app.api.v1.billing import ApplyDiscountRequest, apply_discount

        beta_code = make_mock_discount_code(code="BETA-2026", discount_percent=100, is_beta=True)
        mock_discount.validate_code = AsyncMock(return_value=beta_code)
        mock_org.get_member_role = AsyncMock(return_value="owner")
        mock_sub.get_by_org = AsyncMock(
            return_value=make_mock_subscription(
                tier="pro",
                status="active",
                stripe_subscription_id="sub_123",
                is_manually_assigned=False,
            )
        )

        db = _make_db()
        user = make_mock_user()
        request = ApplyDiscountRequest(
            organization_id=uuid.uuid4(),
            code="BETA-2026",
        )
        with pytest.raises(HTTPException) as exc_info:
            await apply_discount(request, db, user)
        assert exc_info.value.status_code == 400
        assert "Beta codes cannot be applied as discounts" in str(exc_info.value.detail)
