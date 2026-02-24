import asyncio
from logging.config import fileConfig

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
    CommitStatsCache,
    CustomDocJob,
    DiscountCode,
    DiscountRedemption,
    Document,
    DocumentSection,
    DocumentSubsection,
    Feedback,
    InfraComponent,
    Organization,
    OrganizationMember,
    Product,
    ProductAccess,
    ProductApiKey,
    ProgressSummary,
    ReferralCode,
    Repository,
    Subscription,
    UsageSnapshot,
    User,
    UserPreferences,
    WorkItem,
)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = SQLModel.metadata


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
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

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
