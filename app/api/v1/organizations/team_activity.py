"""Organization team activity endpoint.

Aggregates contributor data across all products in an organization,
merging by GitHub author identity and joining with org member records.
"""

import asyncio
import logging
import uuid as uuid_pkg
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_with_rls
from app.api.v1.organizations.helpers import require_org_access
from app.api.v1.organizations.schemas import (
    TeamActivityAggregate,
    TeamActivityResponse,
    TeamMember,
    TeamMemberProduct,
    TeamMemberRecentCommit,
    TeamMemberStats,
)
from app.api.v1.progress.commit_fetcher import fetch_commit_stats, fetch_product_commits
from app.domain import org_member_ops, product_ops
from app.models.user import User
from app.services.github.timeline_types import TimelineEvent

logger = logging.getLogger(__name__)


def _period_string(days: int) -> str:
    """Convert days integer to period string for existing helpers."""
    return f"{days}d"


def _compute_streak(daily_activity: list[dict[str, Any]]) -> int:
    """Compute streak days from daily activity (consecutive days ending today or yesterday)."""
    if not daily_activity:
        return 0

    today = datetime.now(UTC).date()
    yesterday = today - timedelta(days=1)

    # Build a set of dates with commits
    active_dates: set[str] = set()
    for day in daily_activity:
        if day.get("commits", 0) > 0:
            active_dates.add(day["date"])

    # Start counting from today or yesterday
    if today.strftime("%Y-%m-%d") in active_dates:
        current = today
    elif yesterday.strftime("%Y-%m-%d") in active_dates:
        current = yesterday
    else:
        return 0

    streak = 0
    while current.strftime("%Y-%m-%d") in active_dates:
        streak += 1
        current -= timedelta(days=1)

    return streak


def _merge_daily_activity(
    all_daily: list[list[dict[str, Any]]],
    days: int,
) -> list[dict[str, Any]]:
    """Merge multiple daily activity arrays, summing commits per date."""
    merged: dict[str, int] = defaultdict(int)
    for daily in all_daily:
        for entry in daily:
            merged[entry["date"]] += entry.get("commits", 0)

    today = datetime.now(UTC).date()
    result = []
    for i in range(days - 1, -1, -1):
        date = today - timedelta(days=i)
        date_str = date.strftime("%Y-%m-%d")
        result.append({"date": date_str, "commits": merged.get(date_str, 0)})
    return result


def _match_contributor_to_member(
    author: str,
    member_map: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """Match a contributor to an org member by GitHub username.

    Returns the matched member dict or None.
    """
    # Match by github_username (case-insensitive)
    author_lower = author.lower()
    for _uid, member in member_map.items():
        gh = member.get("github_username")
        if gh and gh.lower() == author_lower:
            return member

    return None


async def get_team_activity(
    org_id: uuid_pkg.UUID,
    days: int = Query(14, description="Period in days: 7, 14, 30"),
    sort: str = Query("commits", description="Sort: commits, additions, last_active, name"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> TeamActivityResponse:
    """Get aggregated team activity across all products in an organization."""
    await require_org_access(db, org_id, user)

    # Clamp days to valid range
    if days not in (7, 14, 30):
        days = 14

    period = _period_string(days)

    # 1. Get all products in the org
    products = await product_ops.get_by_organization(db, org_id)
    if not products:
        # Return empty response with org members as idle/pending
        return await _build_empty_response(db, org_id, days)

    # 2. Fetch contributor data per product (in parallel)
    async def fetch_for_product(product: Any) -> tuple[str, str, list[TimelineEvent]]:
        """Fetch and enrich commits for a single product."""
        product_name = product.name or "Unknown"
        product_id_str = str(product.id)
        try:
            result = await fetch_product_commits(
                db=db,
                product_id=product.id,
                current_user=user,
                period=period,
                fetch_limit=200,
            )
            if not result:
                return (product_id_str, product_name, [])

            events = await fetch_commit_stats(db, result.github, result.repos, result.events)
            return (product_id_str, product_name, events)
        except Exception:
            logger.exception(f"Failed to fetch commits for product {product.id}")
            return (product_id_str, product_name, [])

    fetch_results = await asyncio.gather(
        *[fetch_for_product(p) for p in products],
        return_exceptions=True,
    )

    # 3. Merge contributor data across products by author
    # author -> { stats, daily_activities, focus_areas_counts, products, recent_commits, avatar }
    author_data: dict[str, dict[str, Any]] = {}
    products_with_activity: set[str] = set()

    for result in fetch_results:
        if isinstance(result, BaseException):
            logger.warning(f"Product fetch failed: {result}")
            continue

        product_id_str, product_name, events = result
        if not events:
            continue

        products_with_activity.add(product_id_str)

        # Group events by author within this product
        product_authors: dict[str, list[TimelineEvent]] = defaultdict(list)
        for event in events:
            product_authors[event.commit_author].append(event)

        for author, author_events in product_authors.items():
            if author not in author_data:
                author_data[author] = {
                    "commits": 0,
                    "additions": 0,
                    "deletions": 0,
                    "files_changed": 0,
                    "last_active": None,
                    "avatar_url": None,
                    "github_login": None,
                    "daily_activities": [],
                    "focus_area_counts": defaultdict(int),
                    "products": [],
                    "recent_commits": [],
                }

            data = author_data[author]
            product_commits = len(author_events)
            data["commits"] += product_commits
            data["additions"] += sum(e.additions or 0 for e in author_events)
            data["deletions"] += sum(e.deletions or 0 for e in author_events)
            data["files_changed"] += sum(e.files_changed or 0 for e in author_events)

            # Avatar and GitHub login
            if not data["avatar_url"] and author_events:
                data["avatar_url"] = author_events[0].commit_author_avatar
            if not data["github_login"] and author_events:
                data["github_login"] = author_events[0].commit_author_login

            # Last active (newest timestamp)
            author_events.sort(key=lambda e: e.timestamp, reverse=True)
            event_last = author_events[0].timestamp
            if data["last_active"] is None or event_last > data["last_active"]:
                data["last_active"] = event_last

            # Daily activity per product (will merge later)
            daily_counts: dict[str, int] = defaultdict(int)
            for event in author_events:
                date = event.timestamp.split("T")[0]
                daily_counts[date] += 1

            today = datetime.now(UTC).date()
            product_daily = []
            for i in range(days - 1, -1, -1):
                d = today - timedelta(days=i)
                ds = d.strftime("%Y-%m-%d")
                product_daily.append({"date": ds, "commits": daily_counts.get(ds, 0)})
            data["daily_activities"].append(product_daily)

            # Focus areas (repo names)
            for event in author_events:
                data["focus_area_counts"][event.repository_name] += 1

            # Products
            data["products"].append(
                TeamMemberProduct(
                    product_id=product_id_str,
                    product_name=product_name,
                    commits=product_commits,
                )
            )

            # Recent commits with product context
            for e in author_events[:5]:
                data["recent_commits"].append(
                    TeamMemberRecentCommit(
                        sha=e.commit_sha[:7],
                        message=e.commit_message,
                        repository=e.repository_name,
                        product_name=product_name,
                        timestamp=e.timestamp,
                        url=e.commit_url,
                    )
                )

    # 4. Build final stats per contributor
    contributor_stats: dict[
        str, tuple[TeamMemberStats, list[TeamMemberRecentCommit], str | None, str | None]
    ] = {}

    for author, data in author_data.items():
        # Merge daily activity
        merged_daily = _merge_daily_activity(data["daily_activities"], days)
        streak = _compute_streak(merged_daily)

        # Top focus areas
        focus_sorted = sorted(
            data["focus_area_counts"].items(), key=lambda x: x[1], reverse=True
        )
        focus_areas = [area for area, _ in focus_sorted[:5]]

        # Sort recent commits by timestamp, take top 10
        all_recent = sorted(data["recent_commits"], key=lambda c: c.timestamp, reverse=True)[:10]

        stats = TeamMemberStats(
            commits=data["commits"],
            additions=data["additions"],
            deletions=data["deletions"],
            files_changed=data["files_changed"],
            last_active=data["last_active"],
            streak_days=streak,
            daily_activity=merged_daily,
            focus_areas=focus_areas,
            products=data["products"],
        )
        contributor_stats[author] = (stats, all_recent, data["avatar_url"], data["github_login"])

    # 5. Fetch org members and join with contributor data
    org_members = await org_member_ops.get_by_org(db, org_id)

    # Build member lookup: user_id -> member info
    member_map: dict[str, dict[str, Any]] = {}
    for m in org_members:
        uid = str(m.user_id)
        member_map[uid] = {
            "user_id": uid,
            "display_name": m.user.display_name if m.user else None,
            "email": m.user.email if m.user else None,
            "avatar_url": m.user.avatar_url if m.user else None,
            "role": m.role,
            "joined_at": m.joined_at.isoformat() if m.joined_at else None,
            "github_username": m.user.github_username if m.user else None,
            "has_signed_in": (
                m.user.onboarding_completed_at is not None if m.user else False
            ),
        }

    # 6. Match contributors to org members
    matched_member_ids: set[str] = set()
    team_members: list[TeamMember] = []

    for author, (stats, recent_commits, avatar_url, github_login) in contributor_stats.items():
        matched = _match_contributor_to_member(author, member_map)
        if matched:
            uid = matched["user_id"]
            gh_username = matched.get("github_username")
            matched_member_ids.add(uid)
            team_members.append(
                TeamMember(
                    user_id=uid,
                    display_name=matched["display_name"] or author,
                    email=matched["email"],
                    avatar_url=matched["avatar_url"] or avatar_url,
                    role=matched["role"],
                    joined_at=matched["joined_at"],
                    status="active",
                    stats=stats,
                    recent_commits=recent_commits,
                    github_username=gh_username,
                    github_author=None,
                    is_linked=bool(gh_username),
                )
            )
        else:
            # External contributor (not an org member)
            team_members.append(
                TeamMember(
                    user_id=None,
                    display_name=author,
                    email=None,
                    avatar_url=avatar_url,
                    role=None,
                    joined_at=None,
                    status="active",
                    stats=stats,
                    recent_commits=recent_commits,
                    github_username=github_login,
                    github_author=author,
                    is_linked=False,
                )
            )

    # 7. Add idle/pending org members (no matching contributor data)
    for uid, member in member_map.items():
        if uid in matched_member_ids:
            continue

        member_status = "pending" if not member["has_signed_in"] else "idle"
        gh_username = member.get("github_username")

        team_members.append(
            TeamMember(
                user_id=uid,
                display_name=member["display_name"] or member["email"] or "Unknown",
                email=member["email"],
                avatar_url=member["avatar_url"],
                role=member["role"],
                joined_at=member["joined_at"],
                status=member_status,
                stats=None,
                recent_commits=[],
                github_username=gh_username,
                github_author=None,
                is_linked=bool(gh_username),
            )
        )

    # 8. Sort
    team_members = _sort_members(team_members, sort)

    # 9. Compute aggregate
    active_count = sum(1 for m in team_members if m.status == "active")
    total_commits = sum(m.stats.commits for m in team_members if m.stats)
    total_additions = sum(m.stats.additions for m in team_members if m.stats)
    total_deletions = sum(m.stats.deletions for m in team_members if m.stats)

    aggregate = TeamActivityAggregate(
        active_contributors=active_count,
        total_commits=total_commits,
        total_additions=total_additions,
        total_deletions=total_deletions,
        products_touched=len(products_with_activity),
    )

    return TeamActivityResponse(
        period_days=days,
        aggregate=aggregate,
        members=team_members,
    )


def _sort_members(members: list[TeamMember], sort: str) -> list[TeamMember]:
    """Sort team members by the given field."""
    if sort == "additions":
        return sorted(
            members,
            key=lambda m: m.stats.additions if m.stats else 0,
            reverse=True,
        )
    elif sort == "last_active":
        return sorted(
            members,
            key=lambda m: (m.stats.last_active or "") if m.stats else "",
            reverse=True,
        )
    elif sort == "name":
        return sorted(members, key=lambda m: m.display_name.lower())
    else:  # Default: commits
        return sorted(
            members,
            key=lambda m: m.stats.commits if m.stats else 0,
            reverse=True,
        )


async def _build_empty_response(
    db: AsyncSession,
    org_id: uuid_pkg.UUID,
    days: int,
) -> TeamActivityResponse:
    """Build a response with no activity data, just org members as idle/pending."""
    org_members = await org_member_ops.get_by_org(db, org_id)

    members = []
    for m in org_members:
        has_signed_in = m.user.onboarding_completed_at is not None if m.user else False
        gh_username = m.user.github_username if m.user else None
        members.append(
            TeamMember(
                user_id=str(m.user_id),
                display_name=(
                    m.user.display_name
                    if m.user and m.user.display_name
                    else (m.user.email if m.user else "Unknown")
                ),
                email=m.user.email if m.user else None,
                avatar_url=m.user.avatar_url if m.user else None,
                role=m.role,
                joined_at=m.joined_at.isoformat() if m.joined_at else None,
                status="pending" if not has_signed_in else "idle",
                stats=None,
                recent_commits=[],
                github_username=gh_username,
                github_author=None,
                is_linked=bool(gh_username),
            )
        )

    return TeamActivityResponse(
        period_days=days,
        aggregate=TeamActivityAggregate(
            active_contributors=0,
            total_commits=0,
            total_additions=0,
            total_deletions=0,
            products_touched=0,
        ),
        members=members,
    )
