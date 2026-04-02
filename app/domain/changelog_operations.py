import uuid as uuid_pkg
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.changelog import ChangelogCommit, ChangelogEntry


class ChangelogOperations:
    """CRUD operations for ChangelogEntry and ChangelogCommit models.

    Changelog entries are product-scoped resources. Visibility is controlled
    by Product access (RLS). The user_id tracks who triggered generation.
    """

    async def get(
        self,
        db: AsyncSession,
        entry_id: uuid_pkg.UUID,
    ) -> ChangelogEntry | None:
        """Get a changelog entry by ID, with its commits eagerly loaded."""
        statement = (
            select(ChangelogEntry)
            .where(ChangelogEntry.id == entry_id)
            .options(selectinload(ChangelogEntry.commits))
        )
        result = await db.execute(statement)
        return result.scalar_one_or_none()

    async def get_entries_by_product(
        self,
        db: AsyncSession,
        product_id: uuid_pkg.UUID,
        category: str | None = None,
        skip: int = 0,
        limit: int = 50,
    ) -> list[ChangelogEntry]:
        """Get paginated changelog entries for a product, newest first.

        Optionally filter by category (added, changed, fixed, etc.).
        """
        statement = (
            select(ChangelogEntry)
            .where(
                ChangelogEntry.product_id == product_id,
                ChangelogEntry.is_published.is_(True),
            )
            .options(selectinload(ChangelogEntry.commits))
        )

        if category:
            statement = statement.where(ChangelogEntry.category == category)

        statement = (
            statement.order_by(
                ChangelogEntry.entry_date.desc(),
                ChangelogEntry.created_at.desc(),
            )
            .offset(skip)
            .limit(limit)
        )

        result = await db.execute(statement)
        return list(result.scalars().all())

    async def count_by_product(
        self,
        db: AsyncSession,
        product_id: uuid_pkg.UUID,
    ) -> int:
        """Count total published changelog entries for a product."""
        statement = (
            select(func.count())
            .select_from(ChangelogEntry)
            .where(
                ChangelogEntry.product_id == product_id,
                ChangelogEntry.is_published.is_(True),
            )
        )
        result = await db.execute(statement)
        return result.scalar_one()

    async def get_processed_shas(
        self,
        db: AsyncSession,
        product_id: uuid_pkg.UUID,
    ) -> set[str]:
        """Get all commit SHAs already linked to changelog entries for a product.

        Used to determine which commits are 'unprocessed' and need AI grouping.
        """
        statement = (
            select(ChangelogCommit.commit_sha)
            .join(ChangelogEntry, ChangelogCommit.changelog_entry_id == ChangelogEntry.id)
            .where(ChangelogEntry.product_id == product_id)
        )
        result = await db.execute(statement)
        return set(result.scalars().all())

    async def create_entry_with_commits(
        self,
        db: AsyncSession,
        entry_data: dict[str, Any],
        user_id: uuid_pkg.UUID,
        commits: list[dict[str, Any]] | None = None,
    ) -> ChangelogEntry:
        """Create a changelog entry with optional linked commits.

        Atomic: entry and all commits are persisted in the same flush.

        Args:
            entry_data: Fields for the ChangelogEntry (product_id, title, summary, etc.)
            user_id: Who triggered the generation
            commits: List of dicts with commit_sha, commit_message, commit_author,
                     committed_at, repository_id
        """
        entry = ChangelogEntry(**entry_data, user_id=user_id)
        db.add(entry)
        await db.flush()

        if commits:
            for commit_data in commits:
                commit = ChangelogCommit(
                    changelog_entry_id=entry.id,
                    **commit_data,
                )
                db.add(commit)
            await db.flush()

        await db.refresh(entry)
        return entry

    async def update_entry(
        self,
        db: AsyncSession,
        entry: ChangelogEntry,
        update_data: dict[str, Any],
    ) -> ChangelogEntry:
        """Update a changelog entry's editable fields (title, summary, category, etc.)."""
        for field, value in update_data.items():
            setattr(entry, field, value)
        db.add(entry)
        await db.flush()
        await db.refresh(entry)
        return entry

    async def delete_entry(
        self,
        db: AsyncSession,
        entry: ChangelogEntry,
    ) -> bool:
        """Delete a changelog entry and its linked commits.

        Commits are cascade-deleted, making their SHAs available for re-processing.
        """
        await db.delete(entry)
        await db.flush()
        return True


changelog_ops = ChangelogOperations()
