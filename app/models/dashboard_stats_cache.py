"""Dashboard stats cache model for caching aggregate progress metrics."""

import uuid as uuid_pkg
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Column, DateTime, ForeignKey, Index, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlmodel import Field, SQLModel


class DashboardStatsCache(SQLModel, table=True):
    """
    Cached aggregate progress stats for the Dashboard.

    Stores org-level metrics (commits, contributors, daily activity) for a given
    time period. This is a shared cache (not user-scoped) because stats are
    computed from the same commit data for all org members.

    Eliminates the need to call GitHub on every GET /dashboard request.
    """

    __tablename__ = "dashboard_stats_cache"
    __table_args__ = (
        Index(
            "ix_dashboard_stats_cache_org_period",
            "organization_id",
            "period",
            unique=True,
        ),
    )

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
            index=True,
        ),
    )

    period: str = Field(
        max_length=10,
        nullable=False,
        description="Time period: 1d, 2d, 7d, 14d, 30d",
    )

    total_commits: int = Field(default=0, nullable=False)
    total_additions: int = Field(default=0, nullable=False)
    total_deletions: int = Field(default=0, nullable=False)
    unique_contributors: int = Field(default=0, nullable=False)

    daily_activity: list[dict[str, Any]] = Field(
        default_factory=list,
        sa_column=Column(
            JSONB,
            nullable=False,
            server_default=text("'[]'::jsonb"),
            comment="Daily activity: [{date, commits}]",
        ),
    )

    generated_at: datetime = Field(  # type: ignore[call-overload]
        default_factory=lambda: datetime.now(UTC),
        nullable=False,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": text("now()")},
        description="When these stats were last computed",
    )

    created_at: datetime = Field(  # type: ignore[call-overload]
        default_factory=lambda: datetime.now(UTC),
        nullable=False,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": text("now()")},
    )

    updated_at: datetime = Field(  # type: ignore[call-overload]
        default_factory=lambda: datetime.now(UTC),
        nullable=False,
        sa_type=DateTime(timezone=True),
        sa_column_kwargs={"server_default": text("now()")},
    )
