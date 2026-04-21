"""Partner Dashboard API — read-only endpoints for external integrations.

All endpoints are authenticated via org-scoped API keys (trj_org_ prefix)
and return pre-computed cached data. No GitHub API calls or AI generation
happens at request time.

Bypass-then-scope (Phase 3e of cron-role plan): each handler receives a
:class:`PartnerAuthContext` (from ``get_org_api_key`` under BYPASSRLS) and
opens its own ``async_session_maker`` session with RLS context set to
``ctx.effective_user_id`` before any RLS-protected read. The effective
user is the key's creator, falling back to the org's canonical owner if
the creator was deleted — see ``org_api_key_auth.py``.
"""

import uuid as uuid_pkg
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select

from app.api.deps.org_api_key_auth import PartnerAuthContext, require_partner_scope
from app.core.database import async_session_maker
from app.core.rate_limit import PARTNER_READ_LIMIT, rate_limiter
from app.core.rls import set_rls_user_context
from app.domain.dashboard_shipped_operations import dashboard_shipped_ops
from app.domain.dashboard_stats_cache_operations import dashboard_stats_cache_ops
from app.domain.product_operations import product_ops
from app.domain.team_contributor_summary_operations import team_contributor_summary_ops
from app.models.changelog import ChangelogEntry
from app.models.product import Product
from app.models.progress_summary import ProgressSummary
from app.models.repository import Repository
from app.models.user import User

from .partner_schemas import (
    ChangelogFeedEntry,
    ChangelogFeedResponse,
    ContributorItem,
    ContributorProduct,
    ContributorsResponse,
    NarrativeContributorSummary,
    NarrativeProductSummary,
    NarrativeResponse,
    NarrativeStats,
    ProductPortfolioItem,
    ProductPortfolioResponse,
    PulseResponse,
    ShippedContributor,
    ShippedItem,
    ShippedResponse,
)

TRAJAN_APP_BASE = "https://app.trajancloud.com"

router = APIRouter(
    prefix="/partner/org",
    tags=["partner"],
)

VALID_PULSE_PERIODS = {"1d", "2d", "7d", "14d", "30d"}
VALID_SUMMARY_PERIODS = {"7d", "14d", "30d"}


def _validate_period(period: str, valid: set[str]) -> str:
    """Validate a period query parameter, returning 422 for invalid values."""
    if period not in valid:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid period '{period}'. Allowed: {', '.join(sorted(valid))}",
        )
    return period


async def get_partner_key(
    ctx: PartnerAuthContext = Depends(require_partner_scope("partner:read")),
) -> PartnerAuthContext:
    """Dependency: validate partner scope + enforce rate limit."""
    rate_limiter.check_rate_limit(ctx.api_key.id, "partner", PARTNER_READ_LIMIT)
    return ctx


# ---------------------------------------------------------------------------
# 1. Pulse
# ---------------------------------------------------------------------------
@router.get("/pulse", response_model=PulseResponse)
async def get_org_pulse(
    period: str = Query("7d", description="Time period: 1d, 2d, 7d, 14d, 30d"),
    ctx: PartnerAuthContext = Depends(get_partner_key),
) -> PulseResponse:
    """Organisation-level stats strip — health at a glance."""
    period = _validate_period(period, VALID_PULSE_PERIODS)

    org_id = ctx.api_key.organization_id

    async with async_session_maker() as db:
        await set_rls_user_context(db, ctx.effective_user_id)

        cached = await dashboard_stats_cache_ops.get_by_org_period(db, org_id, period)

        product_count_result = await db.execute(
            select(func.count())
            .select_from(Product)
            .where(Product.organization_id == org_id)  # type: ignore[arg-type]
        )
        product_count: int = product_count_result.scalar_one()

        repo_count_result = await db.execute(
            select(func.count())
            .select_from(Repository)
            .join(Product, Repository.product_id == Product.id)  # type: ignore[arg-type]
            .where(Product.organization_id == org_id)  # type: ignore[arg-type]
        )
        repo_count: int = repo_count_result.scalar_one()

        if cached:
            return PulseResponse(
                total_products=product_count,
                total_repositories=repo_count,
                active_contributors=cached.unique_contributors,
                total_commits=cached.total_commits,
                total_additions=cached.total_additions,
                total_deletions=cached.total_deletions,
                daily_activity=cached.daily_activity,
                period=period,
                generated_at=cached.generated_at,
            )

        # No cache yet — return zeroed response
        return PulseResponse(
            total_products=product_count,
            total_repositories=repo_count,
            active_contributors=0,
            total_commits=0,
            total_additions=0,
            total_deletions=0,
            daily_activity=[],
            period=period,
            generated_at=None,
        )


# ---------------------------------------------------------------------------
# 2. Products
# ---------------------------------------------------------------------------
@router.get("/products", response_model=ProductPortfolioResponse)
async def get_org_products(
    period: str = Query("7d", description="Time period: 7d, 14d, 30d"),
    ctx: PartnerAuthContext = Depends(get_partner_key),
) -> ProductPortfolioResponse:
    """Product portfolio — all products with stats and AI summaries."""
    period = _validate_period(period, VALID_SUMMARY_PERIODS)

    org_id = ctx.api_key.organization_id

    async with async_session_maker() as db:
        await set_rls_user_context(db, ctx.effective_user_id)

        products = await product_ops.get_by_organization(db, org_id)
        if not products:
            return ProductPortfolioResponse(products=[], period=period)

        product_ids = [p.id for p in products]
        summary_stmt = select(ProgressSummary).where(
            ProgressSummary.product_id.in_(product_ids),  # type: ignore[attr-defined]
            ProgressSummary.period == period,  # type: ignore[arg-type]
        )
        summary_result = await db.execute(summary_stmt)
        summaries_by_product: dict[uuid_pkg.UUID, ProgressSummary] = {
            s.product_id: s for s in summary_result.scalars().all()
        }

        items: list[ProductPortfolioItem] = []
        for p in products:
            summary = summaries_by_product.get(p.id)
            lead_user: User | None = p.lead_user if hasattr(p, "lead_user") else None

            items.append(
                ProductPortfolioItem(
                    id=p.id,
                    name=p.name,
                    description=p.description,
                    icon=p.icon,
                    color=p.color,
                    lead_name=lead_user.display_name if lead_user else None,
                    lead_avatar_url=lead_user.avatar_url if lead_user else None,
                    repository_count=len(p.repositories) if p.repositories else 0,
                    total_commits=summary.total_commits if summary else 0,
                    total_contributors=summary.total_contributors if summary else 0,
                    total_additions=summary.total_additions if summary else 0,
                    total_deletions=summary.total_deletions if summary else 0,
                    last_activity_at=summary.last_activity_at if summary else None,
                    summary_text=summary.summary_text if summary else None,
                    trajan_url=f"{TRAJAN_APP_BASE}/products/{p.id}",
                )
            )

        return ProductPortfolioResponse(products=items, period=period)


# ---------------------------------------------------------------------------
# 3. Shipped
# ---------------------------------------------------------------------------
@router.get("/shipped", response_model=ShippedResponse)
async def get_org_shipped(
    period: str = Query("7d", description="Time period: 7d, 14d, 30d"),
    ctx: PartnerAuthContext = Depends(get_partner_key),
) -> ShippedResponse:
    """Shipped items feed — categorised work delivered across all products."""
    period = _validate_period(period, VALID_SUMMARY_PERIODS)

    org_id = ctx.api_key.organization_id

    async with async_session_maker() as db:
        await set_rls_user_context(db, ctx.effective_user_id)

        product_stmt = select(Product.id, Product.name).where(  # type: ignore[call-overload]
            Product.organization_id == org_id
        )
        product_result = await db.execute(product_stmt)
        product_rows = product_result.all()
        if not product_rows:
            return ShippedResponse(
                items=[], total_commits=0, merged_prs=0, top_contributors=[], period=period
            )

        product_ids = [row[0] for row in product_rows]
        product_names: dict[uuid_pkg.UUID, str | None] = {row[0]: row[1] for row in product_rows}

        summaries = await dashboard_shipped_ops.get_by_products_period(db, product_ids, period)

        all_items: list[ShippedItem] = []
        total_commits = 0
        total_merged_prs = 0
        contributor_map: dict[str, ShippedContributor] = {}

        for s in summaries:
            for item in s.items:
                all_items.append(
                    ShippedItem(
                        description=item.get("description", ""),
                        category=item.get("category", "other"),
                        product_id=s.product_id,
                        product_name=product_names.get(s.product_id),
                    )
                )
            total_commits += s.total_commits
            total_merged_prs += s.merged_prs

            for c in s.top_contributors:
                name = c.get("author", c.get("name", "Unknown"))
                if name not in contributor_map:
                    contributor_map[name] = ShippedContributor(
                        name=name,
                        avatar_url=c.get("avatar_url"),
                        additions=0,
                        deletions=0,
                    )
                contributor_map[name].additions += c.get("additions", 0)
                contributor_map[name].deletions += c.get("deletions", 0)

        sorted_contributors = sorted(
            contributor_map.values(),
            key=lambda c: c.additions + c.deletions,
            reverse=True,
        )

        return ShippedResponse(
            items=all_items,
            total_commits=total_commits,
            merged_prs=total_merged_prs,
            top_contributors=sorted_contributors,
            period=period,
        )


# ---------------------------------------------------------------------------
# 4. Contributors
# ---------------------------------------------------------------------------
@router.get("/contributors", response_model=ContributorsResponse)
async def get_org_contributors(
    period: str = Query("14d", description="Time period: 7d, 14d, 30d"),
    ctx: PartnerAuthContext = Depends(get_partner_key),
) -> ContributorsResponse:
    """Top contributors with per-person stats and AI narratives."""
    period = _validate_period(period, VALID_SUMMARY_PERIODS)

    org_id = ctx.api_key.organization_id

    async with async_session_maker() as db:
        await set_rls_user_context(db, ctx.effective_user_id)

        team_summary = await team_contributor_summary_ops.get_by_org_period(db, org_id, period)

        if not team_summary or not team_summary.summaries:
            return ContributorsResponse(
                contributors=[],
                team_summary="",
                total_commits=0,
                total_contributors=0,
                period=period,
                generated_at=None,
            )

        product_stmt = select(Product.id, Product.name).where(  # type: ignore[call-overload]
            Product.organization_id == org_id
        )
        product_result = await db.execute(product_stmt)
        product_rows = product_result.all()
        product_ids = [row[0] for row in product_rows]
        product_names: dict[uuid_pkg.UUID, str | None] = {row[0]: row[1] for row in product_rows}

        summary_stmt = select(ProgressSummary).where(
            ProgressSummary.product_id.in_(product_ids),  # type: ignore[attr-defined]
            ProgressSummary.period == period,  # type: ignore[arg-type]
        )
        summary_result = await db.execute(summary_stmt)
        progress_summaries = list(summary_result.scalars().all())

        contributor_products: dict[str, list[ContributorProduct]] = {}
        for ps in progress_summaries:
            if not ps.contributor_summaries:
                continue
            for cs in ps.contributor_summaries:
                name = cs.get("name", "")
                if name not in contributor_products:
                    contributor_products[name] = []
                contributor_products[name].append(
                    ContributorProduct(
                        id=ps.product_id,
                        name=product_names.get(ps.product_id),
                    )
                )

        contributor_items: list[ContributorItem] = []
        for name, data in team_summary.summaries.items():
            contributor_items.append(
                ContributorItem(
                    name=name,
                    avatar_url=None,
                    commit_count=data.get("commit_count", 0),
                    additions=data.get("additions", 0),
                    deletions=data.get("deletions", 0),
                    summary=data.get("summary", ""),
                    products=contributor_products.get(name, []),
                )
            )

        contributor_items.sort(key=lambda c: c.commit_count, reverse=True)

        return ContributorsResponse(
            contributors=contributor_items,
            team_summary=team_summary.team_summary,
            total_commits=team_summary.total_commits,
            total_contributors=team_summary.total_contributors,
            period=period,
            generated_at=team_summary.generated_at,
        )


# ---------------------------------------------------------------------------
# 5. Changelog
# ---------------------------------------------------------------------------

# Map internal categories to partner-facing categories
_CATEGORY_MAP = {
    "added": "feature",
    "changed": "chore",
    "fixed": "fix",
    "removed": "chore",
    "security": "security",
    "infrastructure": "chore",
    "other": "chore",
}


@router.get("/changelog", response_model=ChangelogFeedResponse)
async def get_org_changelog(
    limit: int = Query(20, ge=1, le=100, description="Max entries to return"),
    since: str | None = Query(None, description="Only entries after this date (YYYY-MM-DD)"),
    ctx: PartnerAuthContext = Depends(get_partner_key),
) -> ChangelogFeedResponse:
    """Cross-product changelog feed — what shipped, when, and why."""

    if since:
        try:
            datetime.strptime(since, "%Y-%m-%d")
        except ValueError as err:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Invalid 'since' format. Expected YYYY-MM-DD.",
            ) from err

    org_id = ctx.api_key.organization_id

    async with async_session_maker() as db:
        await set_rls_user_context(db, ctx.effective_user_id)

        stmt = (
            select(ChangelogEntry, Product.name)  # type: ignore[call-overload]
            .join(Product, ChangelogEntry.product_id == Product.id)
            .where(
                Product.organization_id == org_id,
                ChangelogEntry.is_published.is_(True),  # type: ignore[attr-defined]
            )
        )

        if since:
            stmt = stmt.where(ChangelogEntry.entry_date >= since)

        stmt = stmt.order_by(
            ChangelogEntry.entry_date.desc(),  # type: ignore[attr-defined]
            ChangelogEntry.created_at.desc(),  # type: ignore[attr-defined]
        ).limit(limit)

        result = await db.execute(stmt)
        rows = result.all()

        count_stmt = (
            select(func.count())
            .select_from(ChangelogEntry)
            .join(Product, ChangelogEntry.product_id == Product.id)  # type: ignore[arg-type]
            .where(
                Product.organization_id == org_id,  # type: ignore[arg-type]
                ChangelogEntry.is_published.is_(True),  # type: ignore[attr-defined]
            )
        )
        if since:
            count_stmt = count_stmt.where(ChangelogEntry.entry_date >= since)  # type: ignore[arg-type]

        count_result = await db.execute(count_stmt)
        total: int = count_result.scalar_one()

        entries: list[ChangelogFeedEntry] = []
        for row in rows:
            entry: ChangelogEntry = row[0]
            product_name: str | None = row[1]
            entries.append(
                ChangelogFeedEntry(
                    id=entry.id,
                    product_id=entry.product_id,
                    product_name=product_name,
                    version=entry.version,
                    title=entry.title,
                    summary=entry.summary,
                    category=_CATEGORY_MAP.get(entry.category, entry.category),
                    entry_date=entry.entry_date,
                    trajan_url=f"{TRAJAN_APP_BASE}/products/{entry.product_id}/changelog",
                )
            )

        return ChangelogFeedResponse(entries=entries, total=total)


# ---------------------------------------------------------------------------
# 6. Narrative
# ---------------------------------------------------------------------------
@router.get("/narrative", response_model=NarrativeResponse)
async def get_org_narrative(
    period: str = Query("7d", description="Time period: 7d, 14d, 30d"),
    ctx: PartnerAuthContext = Depends(get_partner_key),
) -> NarrativeResponse:
    """AI-generated prose summary — team, product, and contributor narratives."""
    period = _validate_period(period, VALID_SUMMARY_PERIODS)

    org_id = ctx.api_key.organization_id

    async with async_session_maker() as db:
        await set_rls_user_context(db, ctx.effective_user_id)

        team_summary = await team_contributor_summary_ops.get_by_org_period(db, org_id, period)

        product_stmt = select(Product.id, Product.name).where(  # type: ignore[call-overload]
            Product.organization_id == org_id
        )
        product_result = await db.execute(product_stmt)
        product_rows = product_result.all()
        product_ids = [row[0] for row in product_rows]
        product_names: dict[uuid_pkg.UUID, str | None] = {row[0]: row[1] for row in product_rows}

        product_narratives: list[NarrativeProductSummary] = []
        agg_commits = 0
        agg_additions = 0
        agg_deletions = 0
        unique_contributor_names: set[str] = set()
        latest_generated_at = None

        if product_ids:
            summary_stmt = select(ProgressSummary).where(
                ProgressSummary.product_id.in_(product_ids),  # type: ignore[attr-defined]
                ProgressSummary.period == period,  # type: ignore[arg-type]
            )
            summary_result = await db.execute(summary_stmt)
            for ps in summary_result.scalars().all():
                product_narratives.append(
                    NarrativeProductSummary(
                        product_id=ps.product_id,
                        product_name=product_names.get(ps.product_id),
                        summary_text=ps.summary_text,
                        total_commits=ps.total_commits,
                        total_contributors=ps.total_contributors,
                    )
                )
                agg_commits += ps.total_commits
                agg_additions += ps.total_additions
                agg_deletions += ps.total_deletions
                if ps.contributor_summaries:
                    for cs in ps.contributor_summaries:
                        name = cs.get("name", "")
                        if name:
                            unique_contributor_names.add(name)
                if latest_generated_at is None or ps.generated_at > latest_generated_at:
                    latest_generated_at = ps.generated_at

        contributor_narratives: list[NarrativeContributorSummary] = []
        if team_summary and team_summary.summaries:
            for name, data in team_summary.summaries.items():
                contributor_narratives.append(
                    NarrativeContributorSummary(
                        name=name,
                        summary=data.get("summary", ""),
                        commit_count=data.get("commit_count", 0),
                    )
                )
            contributor_narratives.sort(key=lambda c: c.commit_count, reverse=True)

            if team_summary.generated_at and (
                latest_generated_at is None or team_summary.generated_at > latest_generated_at
            ):
                latest_generated_at = team_summary.generated_at

        return NarrativeResponse(
            team_summary=team_summary.team_summary if team_summary else "",
            products=product_narratives,
            contributors=contributor_narratives,
            stats=NarrativeStats(
                total_commits=agg_commits,
                total_contributors=len(unique_contributor_names),
                total_additions=agg_additions,
                total_deletions=agg_deletions,
            ),
            period=period,
            generated_at=latest_generated_at,
        )
