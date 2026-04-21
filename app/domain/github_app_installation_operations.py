"""Domain operations for GitHub App installations.

Follows the BaseOperations pattern. Provides lookup by GitHub's installation_id
and org-scoped queries for the token resolver.
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.base_operations import BaseOperations
from app.models.github_app_installation import (
    GitHubAppInstallation,
    GitHubAppInstallationRepo,
)


class GitHubAppInstallationOperations(BaseOperations[GitHubAppInstallation]):
    def __init__(self) -> None:
        super().__init__(GitHubAppInstallation)

    async def get_by_installation_id(
        self, db: AsyncSession, installation_id: int
    ) -> GitHubAppInstallation | None:
        """Find installation by GitHub's installation ID."""
        result = await db.execute(
            select(GitHubAppInstallation).where(
                GitHubAppInstallation.installation_id == installation_id
            )
        )
        return result.scalar_one_or_none()

    async def get_for_org(
        self, db: AsyncSession, organization_id: UUID
    ) -> GitHubAppInstallation | None:
        """Get a GitHub App installation for an organization.

        Returns the first active installation found. For orgs with multiple
        installations (repos across different GitHub accounts), prefer
        get_for_org_and_account() or get_all_for_org().
        """
        result = await db.execute(
            select(GitHubAppInstallation).where(
                GitHubAppInstallation.organization_id == organization_id
            )
        )
        return result.scalars().first()

    async def get_all_for_org(
        self, db: AsyncSession, organization_id: UUID
    ) -> list[GitHubAppInstallation]:
        """Get all GitHub App installations for an organization."""
        result = await db.execute(
            select(GitHubAppInstallation).where(
                GitHubAppInstallation.organization_id == organization_id
            )
        )
        return list(result.scalars().all())

    async def get_for_org_and_account(
        self, db: AsyncSession, organization_id: UUID, github_account_login: str
    ) -> GitHubAppInstallation | None:
        """Get the installation matching a specific GitHub account within an org.

        Returns the most recently created installation when duplicates exist
        (e.g. stale records left behind when webhook deletes are not processed).
        """
        result = await db.execute(
            select(GitHubAppInstallation)
            .where(
                GitHubAppInstallation.organization_id == organization_id,
                GitHubAppInstallation.github_account_login == github_account_login,
            )
            .order_by(GitHubAppInstallation.created_at.desc())
        )
        return result.scalars().first()

    async def create_installation(self, db: AsyncSession, obj_in: dict) -> GitHubAppInstallation:
        """Create a new installation record (no user_id scope)."""
        db_obj = GitHubAppInstallation(**obj_in)
        db.add(db_obj)
        await db.flush()
        await db.refresh(db_obj)
        return db_obj

    async def delete_by_installation_id(self, db: AsyncSession, installation_id: int) -> None:
        """Delete installation by GitHub's installation ID."""
        installation = await self.get_by_installation_id(db, installation_id)
        if installation:
            await db.delete(installation)
            await db.flush()

    async def suspend(self, db: AsyncSession, installation_id: int) -> None:
        """Mark installation as suspended."""
        installation = await self.get_by_installation_id(db, installation_id)
        if installation:
            installation.suspended_at = datetime.now(UTC)
            db.add(installation)
            await db.flush()

    async def unsuspend(self, db: AsyncSession, installation_id: int) -> None:
        """Clear suspension on installation."""
        installation = await self.get_by_installation_id(db, installation_id)
        if installation:
            installation.suspended_at = None
            db.add(installation)
            await db.flush()

    async def delete_installation(self, db: AsyncSession, id: UUID) -> None:
        """Delete installation by its primary key."""
        installation = await self.get(db, id)
        if installation:
            await db.delete(installation)
            await db.flush()


class GitHubAppInstallationRepoOperations(BaseOperations[GitHubAppInstallationRepo]):
    def __init__(self) -> None:
        super().__init__(GitHubAppInstallationRepo)

    async def upsert(
        self,
        db: AsyncSession,
        installation_db_id: UUID,
        github_repo_id: int,
        full_name: str,
    ) -> GitHubAppInstallationRepo:
        """Create or update a repo record for an installation."""
        result = await db.execute(
            select(GitHubAppInstallationRepo).where(
                GitHubAppInstallationRepo.installation_id == installation_db_id,
                GitHubAppInstallationRepo.github_repo_id == github_repo_id,
            )
        )
        existing = result.scalar_one_or_none()
        if existing:
            existing.full_name = full_name
            db.add(existing)
            await db.flush()
            await db.refresh(existing)
            return existing

        repo = GitHubAppInstallationRepo(
            installation_id=installation_db_id,
            github_repo_id=github_repo_id,
            full_name=full_name,
        )
        db.add(repo)
        await db.flush()
        await db.refresh(repo)
        return repo

    async def delete_by_github_id(
        self, db: AsyncSession, installation_db_id: UUID, github_repo_id: int
    ) -> None:
        """Remove a repo from an installation's selected repos."""
        result = await db.execute(
            select(GitHubAppInstallationRepo).where(
                GitHubAppInstallationRepo.installation_id == installation_db_id,
                GitHubAppInstallationRepo.github_repo_id == github_repo_id,
            )
        )
        repo = result.scalar_one_or_none()
        if repo:
            await db.delete(repo)
            await db.flush()

    async def exists(self, db: AsyncSession, installation_id: UUID, github_repo_id: int) -> bool:
        """Check if a specific repo is in an installation's selected repos."""
        result = await db.execute(
            select(GitHubAppInstallationRepo).where(
                GitHubAppInstallationRepo.installation_id == installation_id,
                GitHubAppInstallationRepo.github_repo_id == github_repo_id,
            )
        )
        return result.scalar_one_or_none() is not None

    async def get_by_installation(
        self, db: AsyncSession, installation_id: UUID
    ) -> list[GitHubAppInstallationRepo]:
        """Get all repos for an installation."""
        result = await db.execute(
            select(GitHubAppInstallationRepo).where(
                GitHubAppInstallationRepo.installation_id == installation_id
            )
        )
        return list(result.scalars().all())


# Singletons
github_app_installation_ops = GitHubAppInstallationOperations()
github_app_installation_repo_ops = GitHubAppInstallationRepoOperations()
