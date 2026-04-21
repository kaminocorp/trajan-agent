"""Team contributor AI summaries endpoint.

Generates and caches per-contributor AI narratives for the team page,
using the existing ContributorSummarizer with org-wide commits.
"""

import asyncio
import logging
import uuid as uuid_pkg
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.api.v1.organizations.helpers import require_org_access
from app.api.v1.progress.commit_fetcher import fetch_commit_stats, fetch_product_commits
from app.core.database import async_session_maker, get_direct_db
from app.core.rls import set_rls_user_context
from app.domain import product_ops, team_contributor_summary_ops
from app.models.user import User
from app.services.progress.summarizer import (
    ContributorCommitData,
    ContributorInput,
    contributor_summarizer,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class ContributorSummaryItemResponse(BaseModel):
    summary: str
    commit_count: int
    additions: int
    deletions: int
    commit_refs: list[dict[str, str]]


class TeamSummariesResponse(BaseModel):
    summaries: dict[str, ContributorSummaryItemResponse]
    team_summary: str
    generated_at: str | None
    is_stale: bool
    is_generating: bool


# ---------------------------------------------------------------------------
# In-memory lock to prevent concurrent regeneration
# ---------------------------------------------------------------------------

_regeneration_locks: dict[str, bool] = {}

# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


async def get_team_summaries(
    org_id: uuid_pkg.UUID,
    days: int = Query(14, description="Period in days: 7, 14, 30"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_direct_db),
) -> TeamSummariesResponse:
    """Get AI-generated per-contributor summaries for the team page."""
    await require_org_access(db, org_id, user)

    if days not in (7, 14, 30):
        days = 14

    period = f"{days}d"
    now = datetime.now(UTC)

    # 1. Check cache
    cached = await team_contributor_summary_ops.get_by_org_period(db, org_id, period)

    if cached:
        age_hours = (now - cached.generated_at).total_seconds() / 3600

        if age_hours < 6:
            # Warm cache — serve immediately
            return _build_response(cached, is_stale=False, is_generating=False)

        if age_hours < 48:
            # Stale — serve cached, trigger background regeneration
            _trigger_background_regeneration(org_id, period, user)
            return _build_response(cached, is_stale=True, is_generating=False)

        # Expired — regenerate synchronously (treat as cache miss)

    # 2. Cache miss or expired — generate fresh
    result = await _generate_team_summaries(db, org_id, period, user)
    return result


# ---------------------------------------------------------------------------
# Background regeneration
# ---------------------------------------------------------------------------


def _trigger_background_regeneration(org_id: uuid_pkg.UUID, period: str, user: User) -> None:
    """Fire-and-forget background regeneration."""
    lock_key = f"{org_id}:{period}"
    if _regeneration_locks.get(lock_key):
        return
    # Set lock BEFORE spawning the task to prevent TOCTOU race
    _regeneration_locks[lock_key] = True
    asyncio.create_task(_background_regenerate(org_id, period, user.id))


async def _background_regenerate(
    org_id: uuid_pkg.UUID, period: str, user_id: uuid_pkg.UUID
) -> None:
    """Background task: regenerate summaries with a fresh DB session.

    Accepts user_id (a plain UUID) instead of a User ORM object to avoid
    DetachedInstanceError — the request session that loaded the User closes
    before this coroutine runs.
    """
    lock_key = f"{org_id}:{period}"
    try:
        async with async_session_maker() as session:
            try:
                # Fresh session → must set RLS context before any RLS-protected query.
                # Pre-cutover this path ran on `direct_session_maker` (postgres, BYPASSRLS)
                # and therefore read cross-tenant data. Post-swap it reads the same view
                # the acting user sees via per-product RLS policies.
                await set_rls_user_context(session, user_id)

                # Re-fetch user in this session's scope
                from sqlalchemy import select

                from app.models.user import User as UserModel

                result = await session.execute(select(UserModel).where(UserModel.id == user_id))
                user = result.scalars().first()
                if not user:
                    logger.error(f"Background regen: user {user_id} not found")
                    return
                await _generate_team_summaries(session, org_id, period, user)
                await session.commit()
            except Exception:
                await session.rollback()
                logger.exception(f"Background summary regeneration failed for org {org_id}")
    finally:
        _regeneration_locks.pop(lock_key, None)


# ---------------------------------------------------------------------------
# Generation logic
# ---------------------------------------------------------------------------


async def _generate_team_summaries(
    db: AsyncSession,
    org_id: uuid_pkg.UUID,
    period: str,
    user: User,
) -> TeamSummariesResponse:
    """Fetch commits, call AI summarizer, cache result."""
    now = datetime.now(UTC)

    # 1. Fetch all products in the org
    products = await product_ops.get_by_organization(db, org_id)
    if not products:
        return TeamSummariesResponse(
            summaries={},
            team_summary="",
            generated_at=now.isoformat(),
            is_stale=False,
            is_generating=False,
        )

    # 2. Fetch commits across all products in parallel
    async def fetch_for_product(product: Any) -> list[dict[str, Any]]:
        try:
            result = await fetch_product_commits(
                db=db,
                product_id=product.id,
                current_user=user,
                period=period,
                fetch_limit=200,
            )
            if not result:
                return []
            events = await fetch_commit_stats(db, result.github, result.repos, result.events)
            return [
                {
                    "author": e.commit_author,
                    "message": e.commit_message,
                    "sha": e.commit_sha[:7],
                    "branch": "",
                    "timestamp": e.timestamp,
                    "additions": e.additions or 0,
                    "deletions": e.deletions or 0,
                    "product_name": product.name or "Unknown",
                }
                for e in events
            ]
        except Exception:
            logger.exception(f"Failed to fetch commits for product {product.id}")
            return []

    all_results = [await fetch_for_product(p) for p in products]

    # 3. Group commits by author
    commits_by_author: dict[str, list[dict[str, Any]]] = defaultdict(list)
    stats_by_author: dict[str, dict[str, int]] = defaultdict(
        lambda: {"additions": 0, "deletions": 0}
    )
    for product_events in all_results:
        for event in product_events:
            author = event["author"]
            commits_by_author[author].append(event)
            stats_by_author[author]["additions"] += event["additions"]
            stats_by_author[author]["deletions"] += event["deletions"]

    total_commits = sum(len(c) for c in commits_by_author.values())
    total_contributors = len(commits_by_author)

    if total_commits == 0:
        empty_summary = _build_team_summary_sentence(0, 0, 0, [])
        await team_contributor_summary_ops.upsert(
            db,
            org_id,
            period,
            summaries={},
            team_summary=empty_summary,
            total_commits=0,
            total_contributors=0,
            last_activity_at=now,
        )
        return TeamSummariesResponse(
            summaries={},
            team_summary=empty_summary,
            generated_at=now.isoformat(),
            is_stale=False,
            is_generating=False,
        )

    # 4. Smart-skip: check if commit count unchanged
    cached = await team_contributor_summary_ops.get_by_org_period(db, org_id, period)
    if cached and cached.total_commits == total_commits:
        await team_contributor_summary_ops.update_last_activity(db, org_id, period, now)
        return _build_response(cached, is_stale=False, is_generating=False)

    # 5. Build ContributorInput for the summarizer
    # Sort by commit count descending, take top 5
    sorted_authors = sorted(commits_by_author.items(), key=lambda x: len(x[1]), reverse=True)

    contributors: list[ContributorCommitData] = []
    for author, author_commits in sorted_authors[:5]:
        contributors.append(
            ContributorCommitData(
                name=author,
                commits=[
                    {
                        "message": c["message"],
                        "sha": c["sha"],
                        "branch": c["branch"],
                        "timestamp": c["timestamp"],
                    }
                    for c in author_commits
                ],
                commit_count=len(author_commits),
                additions=stats_by_author[author]["additions"],
                deletions=stats_by_author[author]["deletions"],
            )
        )

    contributor_input = ContributorInput(
        period=period,
        product_name="(all projects)",
        contributors=contributors,
    )

    # 6. Call the AI summarizer
    try:
        result = await contributor_summarizer.interpret(contributor_input)
    except Exception:
        logger.exception("ContributorSummarizer AI call failed")
        # Return empty summaries on failure rather than crashing
        team_summary = _build_team_summary_sentence(
            total_contributors, total_commits, len(products), sorted_authors
        )
        return TeamSummariesResponse(
            summaries={},
            team_summary=team_summary,
            generated_at=now.isoformat(),
            is_stale=False,
            is_generating=False,
        )

    # 7. Build summaries dict
    summaries_dict: dict[str, Any] = {}
    for item in result.items:
        summaries_dict[item.name] = {
            "summary": item.summary_text,
            "commit_count": item.commit_count,
            "additions": item.additions,
            "deletions": item.deletions,
            "commit_refs": item.commit_refs,
        }

    # 8. Build team summary sentence
    team_summary = _build_team_summary_sentence(
        total_contributors, total_commits, len(products), sorted_authors
    )

    # 9. Upsert to DB
    await team_contributor_summary_ops.upsert(
        db,
        org_id,
        period,
        summaries=summaries_dict,
        team_summary=team_summary,
        total_commits=total_commits,
        total_contributors=total_contributors,
        last_activity_at=now,
    )

    # 10. Build response
    response_summaries: dict[str, ContributorSummaryItemResponse] = {}
    for name, data in summaries_dict.items():
        response_summaries[name] = ContributorSummaryItemResponse(
            summary=data["summary"],
            commit_count=data["commit_count"],
            additions=data["additions"],
            deletions=data["deletions"],
            commit_refs=data["commit_refs"],
        )

    return TeamSummariesResponse(
        summaries=response_summaries,
        team_summary=team_summary,
        generated_at=now.isoformat(),
        is_stale=False,
        is_generating=False,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_response(cached: Any, *, is_stale: bool, is_generating: bool) -> TeamSummariesResponse:
    """Build a response from a cached TeamContributorSummary row."""
    summaries: dict[str, ContributorSummaryItemResponse] = {}
    for name, data in (cached.summaries or {}).items():
        summaries[name] = ContributorSummaryItemResponse(
            summary=data.get("summary", ""),
            commit_count=data.get("commit_count", 0),
            additions=data.get("additions", 0),
            deletions=data.get("deletions", 0),
            commit_refs=data.get("commit_refs", []),
        )
    return TeamSummariesResponse(
        summaries=summaries,
        team_summary=cached.team_summary or "",
        generated_at=cached.generated_at.isoformat() if cached.generated_at else None,
        is_stale=is_stale,
        is_generating=is_generating,
    )


def _build_team_summary_sentence(
    total_contributors: int,
    total_commits: int,
    products_count: int,
    sorted_authors: list[tuple[str, list[Any]]],
) -> str:
    """Build template-based team summary sentence."""
    if total_commits == 0 or total_contributors == 0:
        return ""

    def first_name(name: str) -> str:
        return name.split(" ")[0] if " " in name else name

    if total_contributors == 1 and sorted_authors:
        name = first_name(sorted_authors[0][0])
        return (
            f"{name} made {total_commits} commit{'s' if total_commits != 1 else ''} "
            f"across {products_count} project{'s' if products_count != 1 else ''}."
        )

    base = (
        f"{total_contributors} active contributor{'s' if total_contributors != 1 else ''} "
        f"across {products_count} project{'s' if products_count != 1 else ''}."
    )

    if len(sorted_authors) >= 2 and total_commits > 0:
        top2_commits = len(sorted_authors[0][1]) + len(sorted_authors[1][1])
        top2_pct = round((top2_commits / total_commits) * 100)
        if top2_pct > 60:
            name1 = first_name(sorted_authors[0][0])
            name2 = first_name(sorted_authors[1][0])
            return f"{base} {name1} and {name2} drove {top2_pct}% of changes."

    return base
