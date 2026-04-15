"""Unified token resolution for GitHub API access.

Resolves the best available token for a repository, checking:
1. Per-repo fine-grained token (highest specificity)
2. GitHub App installation token (short-lived, org-level)
3. User's account-wide PAT (legacy fallback)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit import log_token_resolved
from app.core.encryption import token_encryption
from app.models import Repository
from app.services.github.app_auth import github_app_auth

if TYPE_CHECKING:
    from app.domain.github_app_installation_operations import (
        GitHubAppInstallationRepoOperations,
    )
    from app.models.github_app_installation import GitHubAppInstallation

logger = logging.getLogger(__name__)


class TokenResolver:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def resolve_token(
        self,
        repository: Repository,
        user_id: UUID,
    ) -> tuple[str | None, str]:
        """Get the best available token for this repository.

        Returns: (token, method) where method is
            "per_repo_token" | "github_app" | "pat" | "none"
        """
        # Priority 1: Per-repo fine-grained token (highest specificity)
        if getattr(repository, "encrypted_token", None):
            token = token_encryption.decrypt(repository.encrypted_token)
            log_token_resolved(user_id, repository.full_name or "", "per_repo_token")
            return token, "per_repo_token"

        # Priority 2: GitHub App installation for this repo's org
        if github_app_auth.is_configured and repository.product_id:
            app_token = await self._try_app_token(repository)
            if app_token:
                log_token_resolved(user_id, repository.full_name or "", "github_app")
                return app_token, "github_app"

        # Priority 3: User's account-wide PAT
        pat_token = await self._try_user_pat(user_id)
        if pat_token:
            log_token_resolved(user_id, repository.full_name or "", "pat")
            return pat_token, "pat"

        return None, "none"

    async def resolve_token_for_user(self, user_id: UUID) -> tuple[str | None, str]:
        """Resolve a token for general GitHub access (not repo-specific).

        Used for listing repos during import. App tokens are repo-scoped,
        so this prefers PAT for general listing.
        """
        pat_token = await self._try_user_pat(user_id)
        if pat_token:
            return pat_token, "pat"

        return None, "none"

    async def resolve_token_for_org(
        self, organization_id: UUID, user_id: UUID
    ) -> tuple[str | None, str]:
        """Resolve a token with org context.

        Prefers App installation for the org, falls back to user PAT.
        Tries all installations when the org has multiple GitHub accounts.
        """
        from app.domain import github_app_installation_ops

        if github_app_auth.is_configured:
            installations = await github_app_installation_ops.get_all_for_org(
                self.db, organization_id
            )
            for installation in installations:
                if installation.suspended_at:
                    continue
                try:
                    token = await github_app_auth.get_installation_token(
                        installation.installation_id
                    )
                    return token, "github_app"
                except Exception:
                    logger.warning(
                        f"Failed to get App token for installation "
                        f"{installation.installation_id}, trying next"
                    )

        pat_token = await self._try_user_pat(user_id)
        if pat_token:
            return pat_token, "pat"

        return None, "none"

    async def _try_app_token(self, repository: Repository) -> str | None:
        """Try to get a GitHub App installation token for this repo.

        When the org has multiple installations (repos across different GitHub
        accounts), matches the repo owner to the correct installation first,
        then falls back to trying all installations.
        """
        from app.domain import (
            github_app_installation_ops,
            github_app_installation_repo_ops,
            product_ops,
        )

        product = await product_ops.get(self.db, repository.product_id)
        if not product or not product.organization_id:
            return None

        # Extract the GitHub owner from repo full_name (e.g. "owner/repo")
        repo_owner = (repository.full_name or "").split("/")[0] if repository.full_name else None

        # Try matching installation by GitHub account login first
        if repo_owner:
            installation = await github_app_installation_ops.get_for_org_and_account(
                self.db, product.organization_id, repo_owner
            )
            if installation and not installation.suspended_at:
                token = await self._try_installation_token(
                    installation, repository, github_app_installation_repo_ops
                )
                if token:
                    return token

        # Fall back to trying all installations for the org
        installations = await github_app_installation_ops.get_all_for_org(
            self.db, product.organization_id
        )
        for installation in installations:
            if installation.suspended_at:
                continue
            if repo_owner and installation.github_account_login == repo_owner:
                continue  # Already tried above
            token = await self._try_installation_token(
                installation, repository, github_app_installation_repo_ops
            )
            if token:
                return token

        return None

    async def _try_installation_token(
        self,
        installation: GitHubAppInstallation,
        repository: Repository,
        repo_ops: GitHubAppInstallationRepoOperations,
    ) -> str | None:
        """Try to get a token from a specific installation for a repository."""
        if installation.repository_selection != "all":
            if not repository.github_id:
                return None
            has_access = await repo_ops.exists(
                self.db,
                installation_id=installation.id,
                github_repo_id=repository.github_id,
            )
            if not has_access:
                return None

        try:
            return await github_app_auth.get_installation_token(installation.installation_id)
        except Exception:
            logger.warning(f"Failed to get installation token for {installation.installation_id}")
            return None

    async def _try_user_pat(self, user_id: UUID) -> str | None:
        """Try to get the user's personal access token."""
        from app.domain import preferences_ops

        prefs = await preferences_ops.get_by_user_id(self.db, user_id)
        if not prefs or not prefs.github_token:
            return None
        return token_encryption.decrypt(prefs.github_token)
