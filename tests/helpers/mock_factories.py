"""Mock object factories for unit tests.

Creates consistent mock objects that match the real model shapes.
Used in unit tests where the database is fully mocked.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import MagicMock


def make_mock_user(**overrides: object) -> MagicMock:
    user = MagicMock()
    user.id = overrides.get("id", uuid.uuid4())
    user.email = overrides.get("email", "test@example.com")
    user.display_name = overrides.get("display_name", "Test User")
    user.avatar_url = overrides.get("avatar_url")
    user.github_username = overrides.get("github_username")
    user.auth_provider = overrides.get("auth_provider", "email")
    user.is_admin = overrides.get("is_admin", False)
    user.invite_limit = overrides.get("invite_limit", 3)
    user.onboarding_completed_at = overrides.get("onboarding_completed_at")
    user.created_at = overrides.get("created_at", datetime.now(UTC))
    user.updated_at = overrides.get("updated_at")
    return user


def make_mock_organization(**overrides: object) -> MagicMock:
    org = MagicMock()
    org.id = overrides.get("id", uuid.uuid4())
    org.name = overrides.get("name", "__test_org")
    org.slug = overrides.get("slug", "__test-org")
    org.owner_id = overrides.get("owner_id", uuid.uuid4())
    org.settings = overrides.get("settings")
    org.created_at = overrides.get("created_at", datetime.now(UTC))
    org.updated_at = overrides.get("updated_at")
    return org


def make_mock_product(**overrides: object) -> MagicMock:
    product = MagicMock()
    product.id = overrides.get("id", uuid.uuid4())
    product.name = overrides.get("name", "__test_product")
    product.description = overrides.get("description", "Test product")
    product.icon = overrides.get("icon")
    product.color = overrides.get("color")
    product.user_id = overrides.get("user_id", uuid.uuid4())
    product.organization_id = overrides.get("organization_id", uuid.uuid4())
    product.analysis_status = overrides.get("analysis_status")
    product.created_at = overrides.get("created_at", datetime.now(UTC))
    product.updated_at = overrides.get("updated_at", datetime.now(UTC))
    product.repositories = overrides.get("repositories", [])
    product.work_items = overrides.get("work_items", [])
    product.documents = overrides.get("documents", [])
    return product


def make_mock_subscription(tier: str = "indie", **overrides: object) -> MagicMock:
    sub = MagicMock()
    sub.id = overrides.get("id", uuid.uuid4())
    sub.organization_id = overrides.get("organization_id", uuid.uuid4())
    sub.plan_tier = overrides.get("plan_tier", tier)
    sub.status = overrides.get("status", "active")
    sub.base_repo_limit = overrides.get("base_repo_limit", 5)
    sub.stripe_customer_id = overrides.get("stripe_customer_id")
    sub.stripe_subscription_id = overrides.get("stripe_subscription_id")
    sub.is_manually_assigned = overrides.get("is_manually_assigned", False)
    sub.cancel_at_period_end = overrides.get("cancel_at_period_end", False)
    sub.current_period_start = overrides.get("current_period_start")
    sub.current_period_end = overrides.get("current_period_end")
    sub.first_subscribed_at = overrides.get("first_subscribed_at")
    sub.created_at = overrides.get("created_at", datetime.now(UTC))
    sub.updated_at = overrides.get("updated_at")
    return sub


def mock_scalars_result(values: list) -> MagicMock:
    """Create a mock execute() result that yields values via .scalars().all()."""
    result = MagicMock()
    result.scalars.return_value.all.return_value = values
    result.scalars.return_value.first.return_value = values[0] if values else None
    return result


def mock_scalar_result(value: object) -> MagicMock:
    """Create a mock execute() result that yields a single value."""
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    result.scalar.return_value = value
    return result


def make_mock_document(**overrides: object) -> MagicMock:
    doc = MagicMock()
    doc.id = overrides.get("id", uuid.uuid4())
    doc.product_id = overrides.get("product_id", uuid.uuid4())
    doc.title = overrides.get("title", "Test Document")
    doc.type = overrides.get("type", "blueprint")
    doc.folder = overrides.get("folder", {"path": "blueprints"})
    doc.is_generated = overrides.get("is_generated", True)
    doc.sync_status = overrides.get("sync_status")
    doc.github_path = overrides.get("github_path")
    doc.created_by_user_id = overrides.get("created_by_user_id", uuid.uuid4())
    doc.created_at = overrides.get("created_at", datetime.now(UTC))
    doc.updated_at = overrides.get("updated_at", datetime.now(UTC))
    return doc


def make_mock_referral_code(**overrides: object) -> MagicMock:
    code = MagicMock()
    code.id = overrides.get("id", uuid.uuid4())
    code.code = overrides.get("code", "TEST-X7K9")
    code.user_id = overrides.get("user_id", uuid.uuid4())
    code.redeemed_at = overrides.get("redeemed_at")
    code.redeemed_by_user_id = overrides.get("redeemed_by_user_id")
    code.converted_at = overrides.get("converted_at")
    code.is_available = overrides.get("is_available", True)
    code.created_at = overrides.get("created_at", datetime.now(UTC))
    return code


def make_mock_product_access(**overrides: object) -> MagicMock:
    access = MagicMock()
    access.id = overrides.get("id", uuid.uuid4())
    access.product_id = overrides.get("product_id", uuid.uuid4())
    access.user_id = overrides.get("user_id", uuid.uuid4())
    access.access_level = overrides.get("access_level", "editor")
    access.created_at = overrides.get("created_at", datetime.now(UTC))
    return access


def make_mock_preferences(**overrides: object) -> MagicMock:
    prefs = MagicMock()
    prefs.id = overrides.get("id", uuid.uuid4())
    prefs.user_id = overrides.get("user_id", uuid.uuid4())
    prefs.github_token = overrides.get("github_token")
    prefs.email_digest = overrides.get("email_digest", "weekly")
    prefs.digest_product_ids = overrides.get("digest_product_ids")
    prefs.timezone = overrides.get("timezone", "UTC")
    return prefs


def make_mock_org_member(**overrides: object) -> MagicMock:
    member = MagicMock()
    member.id = overrides.get("id", uuid.uuid4())
    member.organization_id = overrides.get("organization_id", uuid.uuid4())
    member.user_id = overrides.get("user_id", uuid.uuid4())
    member.role = overrides.get("role", "member")
    member.invited_by = overrides.get("invited_by")
    member.invited_at = overrides.get("invited_at")
    member.joined_at = overrides.get("joined_at", datetime.now(UTC))
    member.user = overrides.get("user")
    member.organization = overrides.get("organization")
    return member


def make_mock_repository(**overrides: object) -> MagicMock:
    repo = MagicMock()
    repo.id = overrides.get("id", uuid.uuid4())
    repo.name = overrides.get("name", "__test_repo")
    repo.full_name = overrides.get("full_name", "test-org/__test_repo")
    repo.url = overrides.get("url", "https://github.com/test-org/__test_repo")
    repo.product_id = overrides.get("product_id", uuid.uuid4())
    repo.github_id = overrides.get("github_id")
    repo.imported_by_user_id = overrides.get("imported_by_user_id", uuid.uuid4())
    repo.created_at = overrides.get("created_at", datetime.now(UTC))
    repo.updated_at = overrides.get("updated_at", datetime.now(UTC))
    repo.product = overrides.get("product")
    return repo


def make_mock_work_item(**overrides: object) -> MagicMock:
    item = MagicMock()
    item.id = overrides.get("id", uuid.uuid4())
    item.title = overrides.get("title", "__test_work_item")
    item.description = overrides.get("description", "Test work item")
    item.status = overrides.get("status", "open")
    item.type = overrides.get("type", "feature")
    item.priority = overrides.get("priority", "medium")
    item.product_id = overrides.get("product_id", uuid.uuid4())
    item.created_by_user_id = overrides.get("created_by_user_id", uuid.uuid4())
    item.created_at = overrides.get("created_at", datetime.now(UTC))
    item.updated_at = overrides.get("updated_at", datetime.now(UTC))
    return item


def make_mock_feedback(**overrides: object) -> MagicMock:
    fb = MagicMock()
    fb.id = overrides.get("id", uuid.uuid4())
    fb.user_id = overrides.get("user_id", uuid.uuid4())
    fb.category = overrides.get("category", "bug")
    fb.message = overrides.get("message", "Test feedback")
    fb.status = overrides.get("status", "new")
    fb.ai_summary = overrides.get("ai_summary")
    fb.ai_processed_at = overrides.get("ai_processed_at")
    fb.admin_notes = overrides.get("admin_notes")
    fb.created_at = overrides.get("created_at", datetime.now(UTC))
    fb.updated_at = overrides.get("updated_at")
    return fb


def make_mock_announcement(**overrides: object) -> MagicMock:
    ann = MagicMock()
    ann.id = overrides.get("id", uuid.uuid4())
    ann.title = overrides.get("title", "__test_announcement")
    ann.message = overrides.get("message", "Test announcement")
    ann.variant = overrides.get("variant", "info")
    ann.is_active = overrides.get("is_active", True)
    ann.starts_at = overrides.get("starts_at")
    ann.ends_at = overrides.get("ends_at")
    ann.created_at = overrides.get("created_at", datetime.now(UTC))
    return ann


def make_mock_document_section(**overrides: object) -> MagicMock:
    section = MagicMock()
    section.id = overrides.get("id", uuid.uuid4())
    section.product_id = overrides.get("product_id", uuid.uuid4())
    section.name = overrides.get("name", "Technical Documentation")
    section.slug = overrides.get("slug", "technical")
    section.position = overrides.get("position", 0)
    section.icon = overrides.get("icon", "Code")
    section.is_default = overrides.get("is_default", True)
    section.subsections = overrides.get("subsections", [])
    section.created_at = overrides.get("created_at", datetime.now(UTC))
    section.updated_at = overrides.get("updated_at")
    return section


def make_mock_commit_stats_cache(**overrides: object) -> MagicMock:
    cache = MagicMock()
    cache.repository_full_name = overrides.get("repository_full_name", "test-org/repo")
    cache.commit_sha = overrides.get("commit_sha", "abc123")
    cache.additions = overrides.get("additions", 10)
    cache.deletions = overrides.get("deletions", 5)
    cache.files_changed = overrides.get("files_changed", 3)
    return cache


def make_mock_dashboard_shipped(**overrides: object) -> MagicMock:
    shipped = MagicMock()
    shipped.id = overrides.get("id", uuid.uuid4())
    shipped.product_id = overrides.get("product_id", uuid.uuid4())
    shipped.period = overrides.get("period", "7d")
    shipped.items = overrides.get("items", [])
    shipped.has_significant_changes = overrides.get("has_significant_changes", True)
    shipped.total_commits = overrides.get("total_commits", 10)
    shipped.total_additions = overrides.get("total_additions", 100)
    shipped.total_deletions = overrides.get("total_deletions", 50)
    shipped.last_activity_at = overrides.get("last_activity_at")
    shipped.generated_at = overrides.get("generated_at", datetime.now(UTC))
    shipped.created_at = overrides.get("created_at", datetime.now(UTC))
    shipped.updated_at = overrides.get("updated_at")
    return shipped


def make_mock_discount_code(**overrides: object) -> MagicMock:
    code = MagicMock()
    code.id = overrides.get("id", uuid.uuid4())
    code.code = overrides.get("code", "TEST-DISCOUNT")
    code.description = overrides.get("description", "Test discount")
    code.discount_percent = overrides.get("discount_percent", 20)
    code.max_redemptions = overrides.get("max_redemptions")
    code.times_redeemed = overrides.get("times_redeemed", 0)
    code.is_active = overrides.get("is_active", True)
    code.stripe_coupon_id = overrides.get("stripe_coupon_id")
    code.duration = overrides.get("duration", "forever")
    code.duration_in_months = overrides.get("duration_in_months")
    code.is_beta = overrides.get("is_beta", False)
    code.created_at = overrides.get("created_at", datetime.now(UTC))
    code.updated_at = overrides.get("updated_at")
    return code


def make_mock_progress_summary(**overrides: object) -> MagicMock:
    summary = MagicMock()
    summary.id = overrides.get("id", uuid.uuid4())
    summary.product_id = overrides.get("product_id", uuid.uuid4())
    summary.period = overrides.get("period", "7d")
    summary.summary_text = overrides.get("summary_text", "Test summary")
    summary.total_commits = overrides.get("total_commits", 10)
    summary.total_contributors = overrides.get("total_contributors", 2)
    summary.total_additions = overrides.get("total_additions", 100)
    summary.total_deletions = overrides.get("total_deletions", 50)
    summary.last_activity_at = overrides.get("last_activity_at")
    summary.generated_at = overrides.get("generated_at", datetime.now(UTC))
    summary.created_at = overrides.get("created_at", datetime.now(UTC))
    summary.updated_at = overrides.get("updated_at")
    return summary
