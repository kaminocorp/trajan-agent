"""Domain operations for dashboard stats cache."""

import uuid as uuid_pkg
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.dashboard_stats_cache import DashboardStatsCache


class DashboardStatsCacheOperations:
    """
    Operations for cached dashboard aggregate stats.

    Note: This doesn't extend BaseOperations because the cache
    is shared (not user-scoped) and uses upsert patterns.
    """

    def __init__(self) -> None:
        self.model = DashboardStatsCache

    async def get_by_org_period(
        self,
        db: AsyncSession,
        organization_id: uuid_pkg.UUID,
        period: str,
    ) -> DashboardStatsCache | None:
        """Get cached stats for an organization and period."""
        statement = select(DashboardStatsCache).where(
            and_(
                DashboardStatsCache.organization_id == organization_id,  # type: ignore[arg-type]
                DashboardStatsCache.period == period,  # type: ignore[arg-type]
            )
        )
        result = await db.execute(statement)
        return result.scalar_one_or_none()

    def is_fresh(
        self,
        cached: DashboardStatsCache,
        max_age_minutes: int = 10,
    ) -> bool:
        """Check if cached stats are within the freshness threshold."""
        age = datetime.now(UTC) - cached.generated_at.replace(tzinfo=UTC)
        return age < timedelta(minutes=max_age_minutes)

    async def upsert(
        self,
        db: AsyncSession,
        organization_id: uuid_pkg.UUID,
        period: str,
        total_commits: int,
        total_additions: int,
        total_deletions: int,
        unique_contributors: int,
        daily_activity: list[dict[str, Any]],
    ) -> DashboardStatsCache:
        """Create or update cached stats using INSERT ... ON CONFLICT DO UPDATE."""
        now = datetime.now(UTC)

        stmt = (
            insert(self.model)
            .values(
                organization_id=organization_id,
                period=period,
                total_commits=total_commits,
                total_additions=total_additions,
                total_deletions=total_deletions,
                unique_contributors=unique_contributors,
                daily_activity=daily_activity,
                generated_at=now,
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=["organization_id", "period"],
                set_={
                    "total_commits": total_commits,
                    "total_additions": total_additions,
                    "total_deletions": total_deletions,
                    "unique_contributors": unique_contributors,
                    "daily_activity": daily_activity,
                    "generated_at": now,
                    "updated_at": now,
                },
            )
            .returning(DashboardStatsCache)
        )

        result = await db.execute(stmt)
        await db.flush()

        return result.scalar_one()


dashboard_stats_cache_ops = DashboardStatsCacheOperations()
