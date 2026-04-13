"""Changelog generation service.

Fetches commits from GitHub, batches them oldest-first, sends each batch
to Claude for grouping into logical changelog entries, and persists the
results incrementally. Resumable: re-running picks up from unprocessed commits.
"""

import logging
import uuid as uuid_pkg
from datetime import UTC, datetime
from typing import Any, cast

import anthropic

from app.config import settings
from app.domain.changelog_operations import changelog_ops
from app.models.product import Product
from app.models.repository import Repository
from app.services.changelog.types import (
    BatchResult,
    ChangelogGroupedEntry,
    GenerationProgress,
    GenerationResult,
)
from app.services.docs.claude_helpers import MODEL_SONNET, call_with_retry
from app.services.github import GitHubReadOperations
from app.services.github.timeline_types import TimelineEvent

logger = logging.getLogger(__name__)

# Batch size: 75 commits per AI call — balances context usage vs grouping quality
BATCH_SIZE = 75

# Maximum commits to fetch per repo page from GitHub
GITHUB_PAGE_SIZE = 100

VALID_CATEGORIES = {"added", "changed", "fixed", "removed", "security", "infrastructure", "other"}


class ChangelogGenerator:
    """Generates AI-grouped changelog entries from commit history.

    Usage:
        generator = ChangelogGenerator(product, repos, github, user_id)
        result = await generator.generate(db)
    """

    def __init__(
        self,
        product: Product,
        repos: list[Repository],
        github: GitHubReadOperations,
        user_id: uuid_pkg.UUID,
        on_progress: Any | None = None,
    ) -> None:
        self.product = product
        self.repos = repos
        self.github = github
        self.user_id = user_id
        self.on_progress = on_progress  # Optional async callback(GenerationProgress)
        self.client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    async def generate(self, db: Any) -> GenerationResult:
        """Run the full changelog generation pipeline.

        1. Fetch all commits from GitHub (full history, paginated)
        2. Filter out already-processed SHAs
        3. Sort oldest-first, split into batches
        4. For each batch: AI group → persist → update progress
        """
        result = GenerationResult()

        # --- Step 1: Fetch all commits ---
        await self._emit_progress(
            GenerationProgress(stage="fetching", message="Fetching commit history from GitHub...")
        )

        all_events = await self._fetch_all_commits()
        if not all_events:
            result.skipped_reason = "No commits found in linked repositories"
            await self._emit_progress(
                GenerationProgress(stage="complete", message="No commits found.")
            )
            return result

        # --- Step 2: Filter to unprocessed ---
        assert self.product.id is not None
        processed_shas = await changelog_ops.get_processed_shas(db, self.product.id)
        unprocessed = [e for e in all_events if e.commit_sha not in processed_shas]

        if not unprocessed:
            result.skipped_reason = "All commits already processed"
            await self._emit_progress(
                GenerationProgress(stage="complete", message="Changelog is up to date.")
            )
            return result

        # --- Step 3: Sort oldest-first and batch ---
        unprocessed.sort(key=lambda e: e.timestamp)
        batches = self._split_into_batches(unprocessed)
        result.batches_total = len(batches)

        await self._emit_progress(
            GenerationProgress(
                stage="processing",
                message=f"Processing {len(unprocessed)} commits in {len(batches)} batches...",
                batch_total=len(batches),
            )
        )

        # --- Step 4: Process each batch ---
        previous_titles: list[str] = []

        for i, batch in enumerate(batches):
            batch_num = i + 1
            try:
                await self._emit_progress(
                    GenerationProgress(
                        stage="processing",
                        message=f"Processing batch {batch_num} of {len(batches)} "
                        f"({len(batch)} commits)...",
                        batch_current=batch_num,
                        batch_total=len(batches),
                        entries_created=result.entries_created,
                        commits_processed=result.commits_processed,
                    )
                )

                batch_result = await self._process_batch(db, batch, previous_titles)
                result.entries_created += batch_result.entries_created
                result.commits_processed += batch_result.commits_processed
                result.batches_completed = batch_num

                # Fetch titles of entries we just created for next-batch context
                recent_entries = await changelog_ops.get_entries_by_product(
                    db, self.product.id, limit=5
                )
                previous_titles = [e.title for e in recent_entries[:5]]

            except Exception as e:
                error_msg = f"Batch {batch_num} failed: {e}"
                logger.error(error_msg, exc_info=True)
                result.errors.append(error_msg)
                # Continue to next batch — partial progress is preserved
                continue

        await self._emit_progress(
            GenerationProgress(
                stage="complete",
                message=f"Done. Created {result.entries_created} entries "
                f"from {result.commits_processed} commits.",
                batch_current=result.batches_completed,
                batch_total=result.batches_total,
                entries_created=result.entries_created,
                commits_processed=result.commits_processed,
            )
        )

        return result

    # -----------------------------------------------------------------------
    # Commit fetching
    # -----------------------------------------------------------------------

    async def _fetch_all_commits(self) -> list[TimelineEvent]:
        """Fetch full commit history from all repos, paginating through everything."""
        all_events: list[TimelineEvent] = []

        for repo in self.repos:
            if not repo.full_name:
                continue
            owner, name = repo.full_name.split("/")

            sha_cursor: str | None = None
            while True:
                try:
                    commits, has_more = await self.github.get_commits_for_timeline(
                        owner,
                        name,
                        branch=repo.default_branch,
                        per_page=GITHUB_PAGE_SIZE,
                        sha_cursor=sha_cursor,
                    )
                except Exception as e:
                    logger.warning(f"Failed to fetch commits for {repo.full_name}: {e}")
                    break

                if not commits:
                    break

                for commit in commits:
                    all_events.append(
                        TimelineEvent(
                            id=f"commit:{commit['sha']}",
                            event_type="commit",
                            timestamp=commit["commit"]["committer"]["date"],
                            repository_id=str(repo.id),
                            repository_name=repo.name or "",
                            repository_full_name=repo.full_name or "",
                            commit_sha=commit["sha"],
                            commit_message=commit["commit"]["message"].split("\n")[0][:200],
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

                if not has_more:
                    break

                # Use last commit SHA as cursor for next page
                sha_cursor = commits[-1]["sha"]

        return all_events

    # -----------------------------------------------------------------------
    # Batch processing
    # -----------------------------------------------------------------------

    def _split_into_batches(self, events: list[TimelineEvent]) -> list[list[TimelineEvent]]:
        """Split events into batches of BATCH_SIZE."""
        return [events[i : i + BATCH_SIZE] for i in range(0, len(events), BATCH_SIZE)]

    async def _process_batch(
        self,
        db: Any,
        batch: list[TimelineEvent],
        previous_titles: list[str],
    ) -> BatchResult:
        """Process a single batch: AI group → persist."""
        # Build event lookup by SHA for later use
        event_map: dict[str, TimelineEvent] = {e.commit_sha: e for e in batch}

        # Call Claude to group commits
        grouped_entries = await self._call_claude(batch, previous_titles)

        entries_created = 0
        commits_processed = 0

        for grouped in grouped_entries:
            # Validate: only keep SHAs that exist in this batch
            valid_shas = [sha for sha in grouped.commit_shas if sha in event_map]
            if not valid_shas:
                continue

            # Determine entry_date from the latest commit in the group
            latest_timestamp = max(event_map[sha].timestamp for sha in valid_shas)
            entry_date = latest_timestamp[:10]  # YYYY-MM-DD from ISO 8601

            # Build commit records
            commit_records = []
            for sha in valid_shas:
                event = event_map[sha]
                commit_records.append(
                    {
                        "commit_sha": sha,
                        "commit_message": event.commit_message,
                        "commit_author": event.commit_author,
                        "committed_at": event.timestamp,
                        "repository_id": event.repository_id,
                    }
                )

            # Sanitize category
            category = grouped.category.lower().strip()
            if category not in VALID_CATEGORIES:
                category = "other"

            await changelog_ops.create_entry_with_commits(
                db=db,
                entry_data={
                    "product_id": self.product.id,
                    "title": grouped.title[:500],
                    "summary": grouped.summary,
                    "category": category,
                    "entry_date": entry_date,
                    "is_ai_generated": True,
                    "is_published": True,
                },
                user_id=self.user_id,
                commits=commit_records,
            )

            entries_created += 1
            commits_processed += len(valid_shas)

        # Commit the batch transaction
        await db.commit()

        return BatchResult(
            entries_created=entries_created,
            commits_processed=commits_processed,
        )

    # -----------------------------------------------------------------------
    # Claude AI call
    # -----------------------------------------------------------------------

    async def _call_claude(
        self,
        batch: list[TimelineEvent],
        previous_titles: list[str],
    ) -> list[ChangelogGroupedEntry]:
        """Send a batch of commits to Claude and get back grouped entries."""
        prompt = self._build_prompt(batch, previous_titles)
        tool_schema = self._build_tool_schema()

        async def _do_call() -> list[ChangelogGroupedEntry]:
            response = await self.client.messages.create(
                model=MODEL_SONNET,
                max_tokens=4096,
                tools=cast(Any, [tool_schema]),
                tool_choice=cast(Any, {"type": "tool", "name": "save_changelog_entries"}),
                messages=[{"role": "user", "content": prompt}],
            )
            return self._parse_response(response)

        return await call_with_retry(_do_call, operation_name="Changelog grouping")

    def _build_prompt(
        self,
        batch: list[TimelineEvent],
        previous_titles: list[str],
    ) -> str:
        """Build the prompt for Claude to group commits into changelog entries."""
        product_name = self.product.name or "this project"

        # Format commits
        commit_lines: list[str] = []
        for event in batch:
            repo_label = event.repository_name or event.repository_full_name
            commit_lines.append(
                f"- [{event.commit_sha}] ({event.timestamp[:10]}) "
                f"[{repo_label}] {event.commit_author}: {event.commit_message}"
            )
        commits_text = "\n".join(commit_lines)

        # Previous context
        context_section = ""
        if previous_titles:
            titles_text = "\n".join(f"  - {t}" for t in previous_titles)
            context_section = (
                f"\n\nFor context, the most recent changelog entries before this batch are:\n"
                f"{titles_text}\n"
                f"Avoid duplicating these themes unless commits clearly extend them."
            )

        return f"""You are generating a changelog for "{product_name}".

Below is a batch of git commits, ordered oldest to newest. Group these into logical changelog entries — a feature that took multiple commits should be a single entry, while a commit touching unrelated things may warrant separate entries.

COMMITS:
{commits_text}
{context_section}

RULES:
1. Every commit SHA must be assigned to exactly one entry. No orphans, no duplicates.
2. Each entry needs a clear title (e.g., "Feature: Dark Mode Support", "Fix: Auth Redirect Loop").
3. The summary should be 2-4 sentences, understandable by someone who hasn't read the code. No commit-message shorthand.
4. Category must be one of: added, changed, fixed, removed, security, infrastructure, other.
5. Group across repositories when commits are part of the same logical change.
6. Prefer fewer, well-grouped entries over many single-commit entries. But don't force unrelated commits together.

Use the save_changelog_entries tool to return your grouped entries."""

    def _build_tool_schema(self) -> dict[str, Any]:
        """Build the tool schema for structured changelog output."""
        return {
            "name": "save_changelog_entries",
            "description": "Save grouped changelog entries with their associated commit SHAs.",
            "input_schema": {
                "type": "object",
                "required": ["entries"],
                "properties": {
                    "entries": {
                        "type": "array",
                        "description": "List of changelog entries grouped from the commits.",
                        "items": {
                            "type": "object",
                            "required": ["title", "category", "summary", "commit_shas"],
                            "properties": {
                                "title": {
                                    "type": "string",
                                    "description": (
                                        "Entry title, e.g., 'Feature: Dark Mode Support' "
                                        "or 'Fix: Auth Redirect Loop'."
                                    ),
                                },
                                "category": {
                                    "type": "string",
                                    "enum": [
                                        "added",
                                        "changed",
                                        "fixed",
                                        "removed",
                                        "security",
                                        "infrastructure",
                                        "other",
                                    ],
                                    "description": "Change category.",
                                },
                                "summary": {
                                    "type": "string",
                                    "description": (
                                        "2-4 sentence summary of what changed and why, "
                                        "readable by non-developers."
                                    ),
                                },
                                "commit_shas": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": (
                                        "Full 40-char SHA hashes of commits in this entry."
                                    ),
                                },
                            },
                        },
                    },
                },
            },
        }

    def _parse_response(self, response: anthropic.types.Message) -> list[ChangelogGroupedEntry]:
        """Parse Claude's tool-use response into ChangelogGroupedEntry objects."""
        for block in response.content:
            if block.type == "tool_use" and block.name == "save_changelog_entries":
                data = cast(dict[str, Any], block.input)
                entries: list[ChangelogGroupedEntry] = []

                for raw in data.get("entries", []):
                    if isinstance(raw, dict):
                        entries.append(
                            ChangelogGroupedEntry(
                                title=raw.get("title", "Untitled"),
                                category=raw.get("category", "other"),
                                summary=raw.get("summary", ""),
                                commit_shas=raw.get("commit_shas", []),
                            )
                        )

                return entries

        logger.error("Claude response did not contain expected tool use block")
        return []

    # -----------------------------------------------------------------------
    # Progress
    # -----------------------------------------------------------------------

    async def _emit_progress(self, progress: GenerationProgress) -> None:
        """Emit progress update via callback and persist to product."""
        if self.on_progress:
            try:
                await self.on_progress(progress)
            except Exception as e:
                logger.warning(f"Progress callback failed: {e}")

        # Also persist to product for frontend polling
        await self._persist_progress(progress)

    async def _persist_progress(self, progress: GenerationProgress) -> None:
        """Write progress to product.changelog_generation_progress via fresh session."""
        from app.core.database import async_session_maker

        progress_data = {
            "stage": progress.stage,
            "message": progress.message,
            "batch_current": progress.batch_current,
            "batch_total": progress.batch_total,
            "entries_created": progress.entries_created,
            "commits_processed": progress.commits_processed,
            "updated_at": datetime.now(UTC).isoformat(),
        }

        try:
            async with async_session_maker() as session:
                product = await session.get(Product, self.product.id)
                if product:
                    # Reuse docs_generation_progress field with a changelog namespace
                    product.docs_generation_progress = {
                        "type": "changelog",
                        **progress_data,
                    }
                    await session.commit()
        except Exception as e:
            logger.warning(f"Failed to persist changelog progress: {e}")
