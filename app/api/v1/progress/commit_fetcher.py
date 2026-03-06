"""Core commit-fetching logic for Progress API.

This module contains the shared logic for fetching commits from GitHub
repositories, handling renames, and converting to TimelineEvent objects.
It eliminates the ~150-line pattern that was duplicated across 7 endpoints.
"""

import asyncio
import logging
import uuid as uuid_pkg
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import commit_stats_cache_ops, repository_ops
from app.models import User
from app.models.repository import Repository
from app.services.github import GitHubReadOperations
from app.services.github.exceptions import GitHubRepoRenamed
from app.services.github.timeline_types import TimelineEvent

from .utils import get_period_start, handle_repo_rename, resolve_github_token

logger = logging.getLogger(__name__)

# Concurrency limit for fetching commit stats
MAX_CONCURRENT_STAT_FETCHES = 10


@dataclass
class FetchResult:
    """Result of fetching commits for a product."""

    events: list[TimelineEvent]
    repos: list[Repository]
    github: GitHubReadOperations


async def fetch_product_commits(
    db: AsyncSession,
    product_id: uuid_pkg.UUID,
    current_user: User,
    period: str,
    repo_ids: str | None = None,
    fetch_limit: int = 200,
    extended_period: str | None = None,
) -> FetchResult | None:
    """Fetch commits for a product with full rename handling.

    This is the core function that consolidates the ~150-line pattern
    that was previously duplicated across 7 endpoints.

    Args:
        db: Database session
        product_id: Product UUID to fetch commits for
        current_user: Current authenticated user
        period: Time period string (e.g., "7d", "30d")
        repo_ids: Optional comma-separated repository IDs to filter
        fetch_limit: Maximum commits to fetch per repository
        extended_period: If provided, use this period for date filtering
                        (useful for velocity comparison data)

    Returns:
        FetchResult with events, repos, and github client, or None if no data
    """
    # 1. Get GitHub-linked repositories
    repos = await repository_ops.get_github_repos_by_product(db, product_id=product_id)
    if not repos:
        return None

    # 2. Filter repositories if repo_ids provided
    if repo_ids:
        filter_ids = set(repo_ids.split(","))
        repos = [r for r in repos if str(r.id) in filter_ids]
        if not repos:
            return None

    # 3. Resolve GitHub token
    github_token = await resolve_github_token(db, current_user, product_id)
    if not github_token:
        return None

    # 4. Calculate period bounds
    filter_period = extended_period or period
    period_start = get_period_start(filter_period)
    since_str = period_start.strftime("%Y-%m-%dT%H:%M:%SZ")

    # 5. Fetch commits from all repos in parallel
    github = GitHubReadOperations(github_token)

    async def fetch_repo_commits(
        repo: Repository,
    ) -> list[tuple[Repository, dict[str, Any]]]:
        if not repo.full_name:
            return []
        owner, name = repo.full_name.split("/")
        commits, _ = await github.get_commits_for_timeline(
            owner, name, repo.default_branch, per_page=fetch_limit
        )
        return [(repo, c) for c in commits]

    results = await asyncio.gather(
        *[fetch_repo_commits(r) for r in repos],
        return_exceptions=True,
    )

    # 6. Handle renames and flatten results
    all_commits: list[tuple[Repository, dict[str, Any]]] = []
    repos_to_retry: list[Repository] = []

    for i, result in enumerate(results):
        if isinstance(result, GitHubRepoRenamed):
            repo = repos[i]
            updated_repo = await handle_repo_rename(db, github, repo, result)
            if updated_repo:
                repos_to_retry.append(updated_repo)
        elif isinstance(result, BaseException):
            logger.warning(f"Failed to fetch commits for repo: {result}")
            continue
        else:
            all_commits.extend(result)

    # 7. Retry renamed repos
    if repos_to_retry:
        retry_results = await asyncio.gather(
            *[fetch_repo_commits(r) for r in repos_to_retry],
            return_exceptions=True,
        )
        for result in retry_results:
            if isinstance(result, BaseException):
                logger.warning(f"Retry failed for renamed repo: {result}")
                continue
            all_commits.extend(result)

    # 8. Convert to TimelineEvent and filter by date
    events: list[TimelineEvent] = []
    for repo, commit in all_commits:
        timestamp = commit["commit"]["committer"]["date"]

        # Filter by period
        if timestamp < since_str:
            continue

        events.append(
            TimelineEvent(
                id=f"commit:{commit['sha']}",
                event_type="commit",
                timestamp=timestamp,
                repository_id=str(repo.id),
                repository_name=repo.name or "",
                repository_full_name=repo.full_name or "",
                commit_sha=commit["sha"],
                commit_message=commit["commit"]["message"].split("\n")[0][:100],
                commit_author=commit["commit"]["author"]["name"],
                commit_author_login=(
                    commit["author"]["login"] if commit.get("author") else None
                ),
                commit_author_avatar=(
                    commit["author"]["avatar_url"] if commit.get("author") else None
                ),
                commit_url=commit["html_url"],
            )
        )

    if not events:
        return None

    return FetchResult(events=events, repos=repos, github=github)


async def fetch_commit_stats(
    db: AsyncSession,
    github: GitHubReadOperations,
    repos: list[Repository],
    events: list[TimelineEvent],
) -> list[TimelineEvent]:
    """Fetch commit stats (additions, deletions, files) with caching.

    Args:
        db: Database session
        github: GitHub client
        repos: List of repositories
        events: List of TimelineEvent objects to enrich with stats

    Returns:
        The same events list, with stats populated
    """
    # Build repo map
    repo_map: dict[str, tuple[str, str]] = {}
    for repo in repos:
        if repo.full_name:
            owner_name, repo_name = repo.full_name.split("/")
            repo_map[repo.full_name] = (owner_name, repo_name)

    # Bulk fetch from cache
    lookup_keys = [(e.repository_full_name, e.commit_sha) for e in events]
    cached_stats = await commit_stats_cache_ops.get_bulk_by_repo_shas(db, lookup_keys)

    # Identify cache misses and populate hits
    events_needing_fetch: list[TimelineEvent] = []
    for event in events:
        key = (event.repository_full_name, event.commit_sha)
        if key in cached_stats:
            cached = cached_stats[key]
            event.additions = cached.additions
            event.deletions = cached.deletions
            event.files_changed = cached.files_changed
        else:
            events_needing_fetch.append(event)

    # Fetch missing stats from GitHub (with concurrency limit)
    if events_needing_fetch:
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_STAT_FETCHES)
        stats_to_cache: list[dict[str, str | int]] = []

        async def fetch_and_cache_stats(event: TimelineEvent) -> None:
            repo_info = repo_map.get(event.repository_full_name)
            if not repo_info:
                return
            owner_name, repo_name = repo_info

            async with semaphore:
                stats = await github.get_commit_detail(owner_name, repo_name, event.commit_sha)
                if stats:
                    event.additions = stats["additions"]
                    event.deletions = stats["deletions"]
                    event.files_changed = stats["files_changed"]
                    stats_to_cache.append(
                        {
                            "full_name": event.repository_full_name,
                            "sha": event.commit_sha,
                            "additions": stats["additions"],
                            "deletions": stats["deletions"],
                            "files_changed": stats["files_changed"],
                        }
                    )

        await asyncio.gather(
            *[fetch_and_cache_stats(e) for e in events_needing_fetch],
            return_exceptions=True,
        )

        # Bulk insert newly fetched stats into cache
        if stats_to_cache:
            await commit_stats_cache_ops.bulk_upsert(db, stats_to_cache)

    return events
