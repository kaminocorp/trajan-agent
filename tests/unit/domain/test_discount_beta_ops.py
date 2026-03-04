"""Unit tests for beta discount code feature — validation guards and redemption flow."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.domain.discount_operations import DiscountOperations
from tests.helpers.mock_factories import make_mock_discount_code, mock_scalar_result


class TestValidateCodeBetaField:
    """Verify validate_code returns the is_beta field correctly."""

    def setup_method(self):
        self.ops = DiscountOperations()
        self.db = AsyncMock()

    @pytest.mark.asyncio
    async def test_validate_returns_is_beta_true(self):
        beta_code = make_mock_discount_code(code="BETA-2026", discount_percent=100, is_beta=True)
        self.db.execute.return_value = mock_scalar_result(beta_code)

        result = await self.ops.validate_code(self.db, "BETA-2026")
        assert result.is_beta is True

    @pytest.mark.asyncio
    async def test_validate_returns_is_beta_false_for_regular(self):
        regular_code = make_mock_discount_code(code="PROMO-50", discount_percent=50, is_beta=False)
        self.db.execute.return_value = mock_scalar_result(regular_code)

        result = await self.ops.validate_code(self.db, "PROMO-50")
        assert result.is_beta is False


class TestRedeemCodeWithValidatedDiscount:
    """Verify the validated_discount kwarg skips re-validation."""

    def setup_method(self):
        self.ops = DiscountOperations()
        self.db = MagicMock()
        self.db.execute = AsyncMock()
        self.db.flush = AsyncMock()
        self.db.refresh = AsyncMock()
        self.db.add = MagicMock()
        self.org_id = uuid.uuid4()
        self.user_id = uuid.uuid4()

    @pytest.mark.asyncio
    async def test_skips_validation_when_discount_provided(self):
        beta_code = make_mock_discount_code(code="BETA-2026", discount_percent=100, is_beta=True)
        # Mock get_active_discount_for_org to return None (no existing discount)
        with (
            patch.object(self.ops, "get_active_discount_for_org", return_value=None),
            patch.object(self.ops, "validate_code") as mock_validate,
        ):
            await self.ops.redeem_code(
                self.db,
                "BETA-2026",
                self.org_id,
                self.user_id,
                validated_discount=beta_code,
            )
            mock_validate.assert_not_called()

    @pytest.mark.asyncio
    async def test_validates_when_no_discount_provided(self):
        regular_code = make_mock_discount_code(code="PROMO-50", discount_percent=50)
        with (
            patch.object(self.ops, "get_active_discount_for_org", return_value=None),
            patch.object(self.ops, "validate_code", return_value=regular_code) as mock_validate,
        ):
            await self.ops.redeem_code(
                self.db,
                "PROMO-50",
                self.org_id,
                self.user_id,
            )
            mock_validate.assert_called_once_with(self.db, "PROMO-50")

    @pytest.mark.asyncio
    async def test_rejects_org_with_existing_discount(self):
        beta_code = make_mock_discount_code(code="BETA-2026", discount_percent=100, is_beta=True)
        existing_redemption = MagicMock()
        with (
            patch.object(self.ops, "get_active_discount_for_org", return_value=existing_redemption),
            pytest.raises(ValueError, match="already has an active discount"),
        ):
            await self.ops.redeem_code(
                self.db,
                "BETA-2026",
                self.org_id,
                self.user_id,
                validated_discount=beta_code,
            )
