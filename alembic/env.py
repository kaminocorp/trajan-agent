import asyncio
from logging.config import fileConfig

import sqlalchemy as sa
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlmodel import SQLModel

from alembic import context
from app.config import settings

# Import all models to ensure they're registered with SQLModel.metadata
# Using wildcard import to ensure all table models are in SQLModel.metadata
from app.models import (  # noqa: F401
    AppInfo,
    BillingEvent,
    ChangelogCommit,
    ChangelogEntry,
    CommitStatsCache,
    CustomDocJob,
    DashboardShippedSummary,
    DiscountCode,
    DiscountRedemption,
    Document,
    DocumentSection,
    DocumentSubsection,
    Feedback,
    GitHubAppInstallation,
    GitHubAppInstallationRepo,
    InfraComponent,
    Organization,
    OrganizationMember,
    OrgApiKey,
    OrgDigestPreference,
    Product,
    ProductAccess,
    ProductApiKey,
    ProgressSummary,
    ReferralCode,
    Repository,
    Subscription,
    TeamContributorSummary,
    UsageSnapshot,
    User,
    UserPreferences,
    WorkItem,
)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata


def include_object(
    object: sa.schema.SchemaItem,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: sa.schema.SchemaItem | None,
) -> bool:
    """Filter out false-positive PK indexes from autogenerate.

    SQLModel's UUIDMixin causes Alembic to detect phantom indexes on 'id'
    columns that PostgreSQL already indexes via the primary key constraint.
    """
    if type_ == "index" and name and name.endswith("_id") and not reflected:
        # Skip indexes that are just ix_<table>_id on the PK column
        cols = [c.name for c in object.columns]  # type: ignore[attr-defined]
        if cols == ["id"]:
            return False
    return True


def get_url() -> str:
    """Return direct database URL for migrations (DDL requires direct connection)."""
    return settings.database_url_direct


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations in 'online' mode with async engine."""
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = get_url()

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args={"server_settings": {"statement_timeout": "0"}},
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
