"""Per-repository GitHub service factory for docs pipeline.

The docs pipeline (orchestrator, codebase analyzer, sub-agents) previously
used a single GitHubService for all repos. This module provides a factory
that creates per-repo GitHubService instances using TokenResolver, so each
repo can use its own access method (per-repo token, App token, or PAT).

Usage:
    factory = await create_github_service_factory(db, user_id)
    github = await factory(repository)
    tree = await github.get_repo_tree(owner, repo, branch)
"""

import logging
from collections.abc import Awaitable, Callable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.repository import Repository
from app.services.github import GitHubService
from app.services.github.token_resolver import TokenResolver

logger = logging.getLogger(__name__)

# Type alias: async factory that creates a GitHubService for a given repo
GitHubServiceFactory = Callable[[Repository], Awaitable[GitHubService]]


async def create_github_service_factory(
    db: AsyncSession,
    user_id: UUID,
) -> GitHubServiceFactory:
    """Create a factory that produces per-repo GitHubService instances.

    Each call to the factory resolves the best available token for that
    specific repository via TokenResolver (per-repo token > App > PAT).
    """
    resolver = TokenResolver(db)

    async def factory(repository: Repository) -> GitHubService:
        token, method = await resolver.resolve_token(repository, user_id)
        if not token:
            raise ValueError(
                f"No GitHub access for {repository.full_name}. "
                "Install the GitHub App, add a PAT, or link the repo with a token."
            )
        logger.debug(f"Resolved token for {repository.full_name}: method={method}")
        return GitHubService(token)

    return factory


async def get_fallback_github_service(
    db: AsyncSession,
    user_id: UUID,
) -> GitHubService | None:
    """Get a general-purpose GitHubService using the user's PAT.

    Used as fallback for sub-agents that haven't been refactored
    to per-repo resolution (changelog, blueprint, plans agents).
    Returns None if no PAT is available.
    """
    resolver = TokenResolver(db)
    token, _method = await resolver.resolve_token_for_user(user_id)
    if not token:
        return None
    return GitHubService(token)
