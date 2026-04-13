"""Response schemas for the Partner Dashboard API.

All schemas are read-only — no create/update variants.
Used for clean OpenAPI spec generation and response validation.
"""

import uuid as uuid_pkg
from datetime import datetime
from typing import Any

from sqlmodel import Field, SQLModel


# ---------------------------------------------------------------------------
# 1. Pulse
# ---------------------------------------------------------------------------
class PulseResponse(SQLModel):
    """Organisation-level stats strip."""

    total_products: int
    total_repositories: int
    active_contributors: int
    total_commits: int
    total_additions: int
    total_deletions: int
    daily_activity: list[dict[str, Any]]
    period: str
    generated_at: datetime | None


# ---------------------------------------------------------------------------
# 2. Products
# ---------------------------------------------------------------------------
class ProductPortfolioItem(SQLModel):
    """Single product card in the portfolio grid."""

    id: uuid_pkg.UUID
    name: str | None
    description: str | None
    icon: str | None
    color: str | None
    lead_name: str | None
    lead_avatar_url: str | None
    repository_count: int
    total_commits: int
    total_contributors: int
    total_additions: int
    total_deletions: int
    last_activity_at: datetime | None
    summary_text: str | None
    trajan_url: str


class ProductPortfolioResponse(SQLModel):
    """All products with stats and AI summaries."""

    products: list[ProductPortfolioItem]
    period: str


# ---------------------------------------------------------------------------
# 3. Config
# ---------------------------------------------------------------------------
class ConfigRepository(SQLModel):
    """Repository metadata for the config endpoint."""

    id: uuid_pkg.UUID
    name: str | None
    full_name: str | None
    url: str | None
    language: str | None
    default_branch: str | None
    is_private: bool | None
    stars_count: int | None
    forks_count: int | None


class ConfigInfraComponent(SQLModel):
    """Infrastructure component for the config endpoint."""

    id: uuid_pkg.UUID
    name: str | None
    type: str | None
    provider: str | None
    url: str | None
    region: str | None
    description: str | None


class ConfigInfoVariable(SQLModel):
    """Info variable key — never includes the value."""

    key: str | None
    description: str | None
    category: str | None
    tags: list[str] = Field(default_factory=list)


class ProductConfigResponse(SQLModel):
    """Per-product configuration: repos, infra, and variable keys."""

    product_id: uuid_pkg.UUID
    product_name: str | None
    repositories: list[ConfigRepository]
    infrastructure: list[ConfigInfraComponent]
    info_variables: list[ConfigInfoVariable]


# ---------------------------------------------------------------------------
# 4. Shipped
# ---------------------------------------------------------------------------
class ShippedItem(SQLModel):
    """Single shipped item with product attribution."""

    description: str
    category: str
    product_id: uuid_pkg.UUID
    product_name: str | None


class ShippedContributor(SQLModel):
    """Top contributor in the shipped summary."""

    name: str
    avatar_url: str | None
    additions: int
    deletions: int


class ShippedResponse(SQLModel):
    """Shipped items feed across all products."""

    items: list[ShippedItem]
    total_commits: int
    merged_prs: int
    top_contributors: list[ShippedContributor]
    period: str


# ---------------------------------------------------------------------------
# 5. Contributors
# ---------------------------------------------------------------------------
class ContributorProduct(SQLModel):
    """Product a contributor has worked on."""

    id: uuid_pkg.UUID
    name: str | None


class ContributorItem(SQLModel):
    """Single contributor with stats and AI narrative."""

    name: str
    avatar_url: str | None
    commit_count: int
    additions: int
    deletions: int
    summary: str | None
    products: list[ContributorProduct]


class ContributorsResponse(SQLModel):
    """Top contributors with AI narratives."""

    contributors: list[ContributorItem]
    team_summary: str
    total_commits: int
    total_contributors: int
    period: str
    generated_at: datetime | None


# ---------------------------------------------------------------------------
# 6. Changelog
# ---------------------------------------------------------------------------
class ChangelogFeedEntry(SQLModel):
    """Single changelog entry in the cross-product feed."""

    id: uuid_pkg.UUID
    product_id: uuid_pkg.UUID
    product_name: str | None
    version: str | None
    title: str
    summary: str
    category: str
    entry_date: str
    trajan_url: str


class ChangelogFeedResponse(SQLModel):
    """Paginated cross-product changelog feed."""

    entries: list[ChangelogFeedEntry]
    total: int


# ---------------------------------------------------------------------------
# 7. Narrative
# ---------------------------------------------------------------------------
class NarrativeProductSummary(SQLModel):
    """Per-product narrative in the AI progress report."""

    product_id: uuid_pkg.UUID
    product_name: str | None
    summary_text: str | None
    total_commits: int
    total_contributors: int


class NarrativeContributorSummary(SQLModel):
    """Per-contributor narrative in the AI progress report."""

    name: str
    summary: str | None
    commit_count: int


class NarrativeStats(SQLModel):
    """Aggregate stats for the narrative period."""

    total_commits: int
    total_contributors: int
    total_additions: int
    total_deletions: int


class NarrativeResponse(SQLModel):
    """AI-generated prose summary of recent engineering activity."""

    team_summary: str
    products: list[NarrativeProductSummary]
    contributors: list[NarrativeContributorSummary]
    stats: NarrativeStats
    period: str
    generated_at: datetime | None
