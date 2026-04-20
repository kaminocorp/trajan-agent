"""Unit tests for SubscriptionOperations — repo limits, plan assignment, audit."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.domain.subscription_operations import SubscriptionOperations
from tests.helpers.mock_factories import make_mock_subscription


class TestCheckRepoLimit:
    """Tests for repository limit checking and overage calculation."""

    def setup_method(self):
        self.ops = SubscriptionOperations()
        self.db = AsyncMock()
        self.org_id = uuid.uuid4()

    @pytest.mark.asyncio
    @patch.object(SubscriptionOperations, "get_by_org")
    async def test_indie_under_limit_can_add(self, mock_get):
        mock_get.return_value = make_mock_subscription(tier="indie")
        result = await self.ops.check_repo_limit(self.db, self.org_id, current_repo_count=3)
        assert result.can_add is True
        assert result.overage_count == 0
        assert result.overage_cost_cents == 0

    @pytest.mark.asyncio
    @patch.object(SubscriptionOperations, "get_by_org")
    async def test_indie_at_limit_cannot_add(self, mock_get):
        mock_get.return_value = make_mock_subscription(tier="indie")  # base_limit=5
        result = await self.ops.check_repo_limit(self.db, self.org_id, current_repo_count=5)
        assert result.can_add is False
        assert result.overage_count == 1  # Adding 1 more → 6 total, 1 over

    @pytest.mark.asyncio
    @patch.object(SubscriptionOperations, "get_by_org")
    async def test_indie_over_limit_cannot_add(self, mock_get):
        mock_get.return_value = make_mock_subscription(tier="indie")
        result = await self.ops.check_repo_limit(self.db, self.org_id, current_repo_count=7)
        assert result.can_add is False
        assert result.allows_overages is False

    @pytest.mark.asyncio
    @patch.object(SubscriptionOperations, "get_by_org")
    async def test_pro_always_can_add(self, mock_get):
        mock_get.return_value = make_mock_subscription(tier="pro")  # allows_overages=True
        result = await self.ops.check_repo_limit(self.db, self.org_id, current_repo_count=15)
        assert result.can_add is True
        assert result.allows_overages is True

    @pytest.mark.asyncio
    @patch.object(SubscriptionOperations, "get_by_org")
    async def test_pro_calculates_overage_cost(self, mock_get):
        mock_get.return_value = make_mock_subscription(tier="pro")  # base_limit=10
        result = await self.ops.check_repo_limit(self.db, self.org_id, current_repo_count=12)
        # 12 + 1 = 13 total, 3 over base limit of 10
        assert result.overage_count == 3
        assert result.overage_cost_cents == 3000  # 3 * $10 = $30 = 3000 cents

    @pytest.mark.asyncio
    @patch.object(SubscriptionOperations, "get_by_org")
    async def test_no_subscription_defaults_to_observer(self, mock_get):
        mock_get.return_value = None  # No subscription found
        result = await self.ops.check_repo_limit(self.db, self.org_id, current_repo_count=3)
        # "observer" maps to indie → base_limit=5
        assert result.base_limit == 5


class TestAdminAssignPlan:
    """Tests for admin plan assignment bypass."""

    def setup_method(self):
        self.ops = SubscriptionOperations()
        self.db = MagicMock()
        self.db.execute = AsyncMock()
        self.db.flush = AsyncMock()
        self.db.refresh = AsyncMock()
        self.admin_id = uuid.uuid4()

    @pytest.mark.asyncio
    @patch.object(SubscriptionOperations, "log_user_event")
    async def test_sets_plan_fields_and_manual_flag(self, mock_log):
        mock_log.return_value = MagicMock()
        sub = make_mock_subscription(tier="none", status="pending")
        self.db.refresh = AsyncMock()

        await self.ops.admin_assign_plan(self.db, sub, "pro", self.admin_id, "test note")

        assert sub.plan_tier == "pro"
        assert sub.base_repo_limit == 10  # pro limit
        assert sub.is_manually_assigned is True
        assert sub.manually_assigned_by == self.admin_id
        assert sub.status == "active"

    @pytest.mark.asyncio
    @patch.object(SubscriptionOperations, "log_user_event")
    async def test_logs_billing_event_with_correct_data(self, mock_log):
        mock_log.return_value = MagicMock()
        sub = make_mock_subscription(tier="indie")
        self.db.refresh = AsyncMock()

        await self.ops.admin_assign_plan(self.db, sub, "scale", self.admin_id)

        # log_user_event is an external audit call — valid to assert it was called with correct data
        call_kwargs = mock_log.call_args[1]
        assert call_kwargs["new_value"]["plan_tier"] == "scale"
        assert call_kwargs["actor_user_id"] == self.admin_id


class TestIsAgentEnabled:
    """Tests for agent feature gating by tier."""

    def setup_method(self):
        self.ops = SubscriptionOperations()
        self.db = AsyncMock()
        self.org_id = uuid.uuid4()

    @pytest.mark.asyncio
    @patch.object(SubscriptionOperations, "get_by_org")
    async def test_paid_plan_always_enabled(self, mock_get):
        mock_get.return_value = make_mock_subscription(tier="pro")
        result = await self.ops.is_agent_enabled(self.db, self.org_id, current_repo_count=20)
        assert result is True

    @pytest.mark.asyncio
    @patch.object(SubscriptionOperations, "get_by_org")
    async def test_indie_under_limit_enabled(self, mock_get):
        mock_get.return_value = make_mock_subscription(tier="indie")
        result = await self.ops.is_agent_enabled(self.db, self.org_id, current_repo_count=3)
        assert result is True
