"""Auto-progress orchestrator for daily AI summary generation.

Main entry point for the cron job. Iterates over all organizations with
auto_progress_enabled, checks for new activity, and regenerates summaries
only when new commits exist.
"""

import asyncio
import logging
import time
import uuid as uuid_pkg
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.product import Product
from app.services.github import GitHubReadOperations
from app.services.progress.activity_checker import activity_checker
from app.services.progress.token_resolver import token_resolver

logger = logging.getLogger(__name__)

# Safety caps
MAX_PRODUCTS_PER_ORG = 50
PRODUCT_TIMEOUT_SECONDS = 30
TOTAL_JOB_TIMEOUT_SECONDS = 600  # 10 minutes max


@dataclass
class AutoProgressReport:
    """Summary of an auto-progress run (for logging/monitoring)."""

    orgs_processed: int = 0
    products_regenerated: int = 0
    products_skipped: int = 0
    products_failed: int = 0
    errors: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0


@dataclass(frozen=True)
class ProductWorkTarget:
    """Immutable per-product work unit from :func:`enumerate_eligible_products`.

    Carries only primitives + the decrypted GitHub token (resolved
    during bootstrap because ``preferences`` is RLS-protected).
    The scoped half reloads ``Product`` / ``Repository`` rows under
    RLS, so no ORM object crosses sessions.
    """

    product_id: uuid_pkg.UUID
    organization_id: uuid_pkg.UUID
    owner_user_id: uuid_pkg.UUID
    github_token: str


class AutoProgressGenerator:
    """Orchestrator that runs auto-progress for all eligible organizations."""

    async def run_for_all_orgs(
        self,
        db: AsyncSession,
    ) -> AutoProgressReport:
        """
        Main entry point for the cron job.

        1. Find all orgs with auto_progress_enabled = true
        2. For each org, resolve a GitHub token and process products
        3. Return a report of what was generated/skipped
        """
        from app.domain import organization_ops

        start = time.monotonic()
        report = AutoProgressReport()

        orgs = await organization_ops.get_orgs_with_auto_progress(db)
        logger.info(f"[auto-progress] Found {len(orgs)} orgs with auto-progress enabled")

        try:
            async with asyncio.timeout(TOTAL_JOB_TIMEOUT_SECONDS):
                for org in orgs:
                    try:
                        github_token = await token_resolver.resolve_for_org(db, org.id)
                        if not github_token:
                            logger.warning(
                                f"[auto-progress] Org {org.id} ({org.name}): "
                                "no GitHub token available, skipping"
                            )
                            continue

                        await self._process_org(db, org.id, github_token, report)
                        report.orgs_processed += 1

                    except Exception as e:
                        error_msg = f"Org {org.id} ({org.name}): {e}"
                        logger.error(f"[auto-progress] {error_msg}")
                        report.errors.append(error_msg)
        except TimeoutError:
            error_msg = f"Total job timeout ({TOTAL_JOB_TIMEOUT_SECONDS}s) exceeded"
            logger.error(f"[auto-progress] {error_msg}")
            report.errors.append(error_msg)

        report.duration_seconds = round(time.monotonic() - start, 2)

        logger.info(
            f"[auto-progress] Completed: {report.orgs_processed} orgs, "
            f"{report.products_regenerated} regenerated, "
            f"{report.products_skipped} skipped, "
            f"{report.products_failed} failed "
            f"({report.duration_seconds}s)"
        )

        return report

    async def _process_org(
        self,
        db: AsyncSession,
        org_id: uuid_pkg.UUID,
        github_token: str,
        report: AutoProgressReport,
    ) -> None:
        """Process all products in an organization."""
        from app.domain import product_ops, repository_ops

        products = await product_ops.get_by_organization(db, org_id)
        products = products[:MAX_PRODUCTS_PER_ORG]

        github = GitHubReadOperations(github_token)

        for product in products:
            try:
                async with asyncio.timeout(PRODUCT_TIMEOUT_SECONDS):
                    repos = await repository_ops.get_github_repos_by_product(db, product.id)
                    if not repos:
                        report.products_skipped += 1
                        continue

                    regenerated = await self._process_product(db, product, repos, github)
                    if regenerated:
                        report.products_regenerated += 1
                    else:
                        report.products_skipped += 1

            except TimeoutError:
                error_msg = (
                    f"Product {product.id} ({product.name}): timeout ({PRODUCT_TIMEOUT_SECONDS}s)"
                )
                logger.error(f"[auto-progress] {error_msg}")
                report.errors.append(error_msg)
                report.products_failed += 1
            except Exception as e:
                error_msg = f"Product {product.id} ({product.name}): {e}"
                logger.error(f"[auto-progress] {error_msg}")
                report.errors.append(error_msg)
                report.products_failed += 1

    async def _process_product(
        self,
        db: AsyncSession,
        product: Product,
        repos: list,
        github: GitHubReadOperations,
    ) -> bool:
        """
        Process a single product.

        1. Check latest commit date via ActivityChecker
        2. Compare with stored last_activity_at
        3. If newer commits exist → regenerate both summaries (7d)
        4. If daily subscribers exist → also generate 1d summaries
        5. If no new commits → skip (return False)

        Returns True if summaries were regenerated, False if skipped.
        """
        from app.domain import (
            dashboard_shipped_ops,
            progress_summary_ops,
        )

        # Default period for auto-generation
        progress_period = "7d"

        # 1. Check latest commit date (lightweight — per_page=1 per repo)
        latest_commit_date = await activity_checker.get_latest_commit_date(repos, github)

        if latest_commit_date is None:
            logger.debug(f"[auto-progress] Product {product.id}: no commits found")
            return False

        # 2. Compare with stored last_activity_at
        existing = await progress_summary_ops.get_by_product_period(db, product.id, progress_period)

        if (
            existing
            and existing.last_activity_at
            and latest_commit_date <= existing.last_activity_at
        ):
            logger.debug(f"[auto-progress] Product {product.id}: no new activity, skipping")
            return False

        # 3. New activity detected — fetch commits for the 7d window
        logger.info(
            f"[auto-progress] Product {product.id} ({product.name}): "
            "new activity detected, regenerating"
        )

        period_start_7d = _get_period_start(progress_period)
        since_str = period_start_7d.strftime("%Y-%m-%dT%H:%M:%SZ")

        all_commits_raw: list[dict] = []
        # Track which repo each commit came from (for branch info)
        commit_repo_map: dict[str, str] = {}  # sha → repo default_branch

        for repo in repos:
            if not repo.full_name:
                continue
            try:
                owner, name = repo.full_name.split("/")
                commits, _ = await github.get_commits_for_timeline(
                    owner, name, repo.default_branch, per_page=200
                )
                for c in commits:
                    if c["commit"]["committer"]["date"] >= since_str:
                        all_commits_raw.append(c)
                        commit_repo_map[c["sha"]] = repo.default_branch or "main"
            except Exception as e:
                logger.warning(f"[auto-progress] Failed to fetch commits for {repo.full_name}: {e}")

        if not all_commits_raw:
            # Update last_activity_at even if no commits in period
            await progress_summary_ops.update_last_activity(
                db, product.id, progress_period, latest_commit_date
            )
            await dashboard_shipped_ops.update_last_activity(
                db, product.id, progress_period, latest_commit_date
            )
            await db.commit()
            return False

        # --- Generate 7d summaries (always) ---
        await self._generate_summaries_for_period(
            db=db,
            product=product,
            all_commits_raw=all_commits_raw,
            commit_repo_map=commit_repo_map,
            period="7d",
            latest_commit_date=latest_commit_date,
        )

        # --- Conditionally generate 1d summaries for daily digest subscribers ---
        has_daily_subs = await _has_daily_subscribers_for_product(db, product)
        if has_daily_subs:
            period_start_1d = _get_period_start("1d")
            since_1d_str = period_start_1d.strftime("%Y-%m-%dT%H:%M:%SZ")
            commits_1d = [
                c for c in all_commits_raw if c["commit"]["committer"]["date"] >= since_1d_str
            ]

            if commits_1d:
                logger.info(
                    f"[auto-progress] Product {product.id}: generating 1d summaries "
                    f"({len(commits_1d)} commits, daily subscribers exist)"
                )
                await self._generate_summaries_for_period(
                    db=db,
                    product=product,
                    all_commits_raw=commits_1d,
                    commit_repo_map=commit_repo_map,
                    period="1d",
                    latest_commit_date=latest_commit_date,
                    use_haiku=True,
                )

        # Commit per-product so one failure doesn't roll back other products' summaries.
        await db.commit()
        return True

    async def _generate_summaries_for_period(
        self,
        db: AsyncSession,
        product: Product,
        all_commits_raw: list[dict[str, Any]],
        commit_repo_map: dict[str, str],
        period: str,
        latest_commit_date: datetime,
        use_haiku: bool = False,
    ) -> None:
        """Generate progress, shipped, and contributor summaries for a given period.

        Args:
            use_haiku: If True, use Haiku model (for daily summaries to reduce cost).
        """
        from app.domain import (
            dashboard_shipped_ops,
            progress_summary_ops,
        )
        from app.services.progress.shipped_summarizer import (
            CommitInfo,
            ShippedAnalysisInput,
            shipped_summarizer,
        )
        from app.services.progress.summarizer import (
            ContributorCommitData,
            ContributorInput,
            ProgressData,
            contributor_summarizer,
            progress_summarizer,
        )

        # Collect contributor info from raw commits
        contributors: set[str] = set()
        commits_by_author: dict[str, list[dict[str, Any]]] = {}
        for c in all_commits_raw:
            author = c["commit"]["author"]["name"]
            contributors.add(author)
            commits_by_author.setdefault(author, []).append(c)

        # --- Generate Progress AI Summary ---
        contributor_summaries_data: list[dict[str, Any]] | None = None
        try:
            recent_commits_data = []
            for c in all_commits_raw[:10]:
                msg = c["commit"]["message"].split("\n")[0][:100]
                author = c["commit"]["author"]["name"]
                sha = c["sha"]
                branch = commit_repo_map.get(sha, "")
                recent_commits_data.append(
                    {
                        "message": msg,
                        "author": author,
                        "sha": sha,
                        "branch": branch,
                    }
                )

            progress_data = ProgressData(
                period=period,
                total_commits=len(all_commits_raw),
                total_contributors=len(contributors),
                total_additions=0,
                total_deletions=0,
                focus_areas=[],
                top_contributors=[
                    {"author": a, "commits": len(commits_by_author.get(a, []))}
                    for a in list(contributors)[:5]
                ],
                recent_commits=recent_commits_data,
            )

            haiku_model = "claude-haiku-4-5-20251001" if use_haiku else None
            narrative = await progress_summarizer.interpret(
                progress_data, model_override=haiku_model
            )

            # --- Generate Per-Contributor Summaries ---
            try:
                contrib_data = [
                    ContributorCommitData(
                        name=author,
                        commits=[
                            {
                                "message": c["commit"]["message"].split("\n")[0][:100],
                                "sha": c["sha"],
                                "branch": commit_repo_map.get(c["sha"], ""),
                                "timestamp": c["commit"]["committer"]["date"],
                            }
                            for c in author_commits
                        ],
                        commit_count=len(author_commits),
                    )
                    for author, author_commits in sorted(
                        commits_by_author.items(),
                        key=lambda x: len(x[1]),
                        reverse=True,
                    )[:5]
                ]

                contrib_input = ContributorInput(
                    period=period,
                    product_name=product.name or "Unnamed",
                    contributors=contrib_data,
                )

                contrib_result = await contributor_summarizer.interpret(
                    contrib_input, model_override=haiku_model
                )

                contributor_summaries_data = [
                    {
                        "name": item.name,
                        "summary_text": item.summary_text,
                        "commit_count": item.commit_count,
                        "additions": item.additions,
                        "deletions": item.deletions,
                        "commit_refs": item.commit_refs,
                    }
                    for item in contrib_result.items
                ]

            except Exception as e:
                logger.warning(
                    f"[auto-progress] Contributor summaries failed for {product.id} ({period}): {e}"
                )

            await progress_summary_ops.upsert(
                db=db,
                product_id=product.id,
                period=period,
                summary_text=narrative.summary,
                total_commits=len(all_commits_raw),
                total_contributors=len(contributors),
                last_activity_at=latest_commit_date,
                contributor_summaries=contributor_summaries_data,
            )

        except Exception as e:
            logger.error(
                f"[auto-progress] Progress summary failed for {product.id} ({period}): {e}"
            )

        # --- Generate Dashboard Shipped Summary ---
        try:
            commit_infos = [
                CommitInfo(
                    sha=c["sha"],
                    message=c["commit"]["message"].split("\n")[0][:200],
                    author=c["commit"]["author"]["name"],
                    timestamp=c["commit"]["committer"]["date"],
                    files=[],
                )
                for c in all_commits_raw
            ]

            input_data = ShippedAnalysisInput(
                product_id=product.id,
                product_name=product.name or "Unnamed",
                period=period,
                commits=commit_infos,
            )

            summary = await shipped_summarizer.interpret(input_data, model_override=haiku_model)

            items_as_dicts = [
                {"description": item.description, "category": item.category}
                for item in summary.items
            ]
            await dashboard_shipped_ops.upsert(
                db=db,
                product_id=product.id,
                period=period,
                items=items_as_dicts,
                has_significant_changes=summary.has_significant_changes,
                total_commits=len(all_commits_raw),
                last_activity_at=latest_commit_date,
            )

        except Exception as e:
            logger.error(f"[auto-progress] Shipped summary failed for {product.id} ({period}): {e}")


async def _has_daily_subscribers_for_product(
    db: AsyncSession,
    product: Product,
) -> bool:
    """Check if any user has a daily digest enabled for this product's org.

    Queries OrgDigestPreference rows for the product's organization
    where email_digest == 'daily', and either no product filter is set
    or the product is in the user's filter list.
    """
    from app.models.org_digest_preference import OrgDigestPreference

    if not product.organization_id:
        return False

    stmt = (
        select(func.count())
        .select_from(OrgDigestPreference)
        .where(
            OrgDigestPreference.organization_id == product.organization_id,  # type: ignore[arg-type]
            OrgDigestPreference.email_digest == "daily",  # type: ignore[arg-type]
            or_(
                OrgDigestPreference.digest_product_ids.is_(None),  # type: ignore[union-attr]
                OrgDigestPreference.digest_product_ids.cast(JSONB).contains(  # type: ignore[union-attr]
                    [str(product.id)]
                ),
            ),
        )
    )
    result = await db.execute(stmt)
    count = result.scalar() or 0
    return count > 0


def _get_period_start(period: str) -> datetime:
    """Convert period string to start datetime (duplicated from progress.py to avoid circular)."""
    from datetime import timedelta

    now = datetime.now(UTC)
    period_map = {
        "1d": timedelta(days=1),
        "24h": timedelta(hours=24),
        "48h": timedelta(hours=48),
        "7d": timedelta(days=7),
        "14d": timedelta(days=14),
        "30d": timedelta(days=30),
        "90d": timedelta(days=90),
        "365d": timedelta(days=365),
    }
    delta = period_map.get(period, timedelta(days=7))
    return now - delta


auto_progress_generator = AutoProgressGenerator()


# ---------------------------------------------------------------------------
# Bypass-then-scope entry points (Phase 2c)
#
# ``run_for_all_orgs`` above is preserved as a single-session wrapper for
# tests and dev-only callers. The scheduler path uses the two functions
# below: bootstrap enumeration on ``cron_session_maker`` (BYPASSRLS) and
# per-product regeneration on an RLS-scoped ``trajan_app`` session whose
# ``app.current_user_id`` has been set to the product's owning user.
# ---------------------------------------------------------------------------


async def enumerate_eligible_products(
    cron_db: AsyncSession,
) -> list[ProductWorkTarget]:
    """Bootstrap half: cross-tenant enumeration of auto-progress work.

    Runs on ``cron_session_maker`` (BYPASSRLS) because:

    1. ``organizations.settings['auto_progress_enabled']`` lookup is
       cross-tenant by definition — no single user has visibility
       into every org with the flag set.
    2. GitHub token resolution reads ``preferences`` (RLS-protected,
       token-per-member) to find any org owner/admin with a stored
       token. Under ``trajan_app`` with no context, zero tokens
       resolve and every auto-progress run silently no-ops.

    Per-org token resolution is serial rather than batched: the
    resolver walks members in role-priority order and short-circuits
    at the first valid token. Fan-out parallelism wouldn't buy much
    for ~N orgs with ~few admins each.
    """
    from app.domain import organization_ops, product_ops, repository_ops

    targets: list[ProductWorkTarget] = []

    orgs = await organization_ops.get_orgs_with_auto_progress(cron_db)
    logger.info(f"[auto-progress] Bootstrap: {len(orgs)} orgs with auto-progress enabled")

    for org in orgs:
        github_token = await token_resolver.resolve_for_org(cron_db, org.id)
        if not github_token:
            logger.warning(
                f"[auto-progress] Org {org.id} ({org.name}): no GitHub token, skipping"
            )
            continue

        products = await product_ops.get_by_organization(cron_db, org.id)
        for product in products[:MAX_PRODUCTS_PER_ORG]:
            repos = await repository_ops.get_github_repos_by_product(
                cron_db, product.id
            )
            if not repos:
                continue
            targets.append(
                ProductWorkTarget(
                    product_id=product.id,
                    organization_id=org.id,
                    owner_user_id=org.owner_id,
                    github_token=github_token,
                )
            )

    logger.info(f"[auto-progress] Bootstrap: enumerated {len(targets)} eligible products")
    return targets


async def regenerate_for_product(
    db: AsyncSession,
    target: ProductWorkTarget,
    report: AutoProgressReport,
) -> bool:
    """Scoped half: regenerate summaries for a single product.

    **Contract:** caller has set RLS context to
    ``target.owner_user_id`` on ``db``. Every read below
    (``products``, ``repositories``, ``progress_summaries``,
    ``dashboard_shipped_summaries``, ``org_digest_preferences``) is
    evaluated by policies against that user. The
    RLS-rehydration listener in ``database.py`` keeps the context
    alive across the mid-flight ``commit()``s that
    ``_process_product`` issues (per-period commits isolate one
    product's failure from siblings).

    Returns True iff summaries were regenerated (not skipped).
    """
    from app.domain import product_ops, repository_ops

    product = await product_ops.get(db, target.product_id)
    if product is None:
        logger.warning(
            f"[auto-progress] Product {target.product_id} invisible under owner "
            f"RLS context — skipping"
        )
        report.products_skipped += 1
        return False

    repos = await repository_ops.get_github_repos_by_product(db, target.product_id)
    if not repos:
        report.products_skipped += 1
        return False

    github = GitHubReadOperations(target.github_token)
    try:
        async with asyncio.timeout(PRODUCT_TIMEOUT_SECONDS):
            regenerated = await auto_progress_generator._process_product(
                db, product, repos, github
            )
    except TimeoutError:
        error_msg = f"Product {target.product_id}: timeout ({PRODUCT_TIMEOUT_SECONDS}s)"
        logger.error(f"[auto-progress] {error_msg}")
        report.errors.append(error_msg)
        report.products_failed += 1
        return False

    if regenerated:
        report.products_regenerated += 1
    else:
        report.products_skipped += 1
    return regenerated
