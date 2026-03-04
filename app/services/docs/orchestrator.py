"""
DocumentOrchestrator - Coordinates the entire documentation generation process.

This is the top-level orchestrator that:
1. Scans repositories for existing documentation
2. Imports existing docs into our folder structure
3. Performs deep codebase analysis (v2)
4. Plans documentation using Claude Opus 4.5 (v2)
5. Generates documents sequentially from the plan (v2)
6. Coordinates progress updates for frontend polling

V2 Flow (default):
    Import existing → Analyze codebase → Plan docs → Generate sequentially

V1 Flow (legacy, use_v2=False):
    Import existing → ChangelogAgent → BlueprintAgent → PlansAgent
"""

import asyncio
import logging
from collections.abc import Coroutine
from datetime import UTC, datetime
from typing import Any, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.document_operations import document_ops
from app.domain.repository_operations import repository_ops
from app.models.document import Document
from app.models.product import Product
from app.models.repository import Repository
from app.services.docs.blueprint_agent import BlueprintAgent
from app.services.docs.changelog_agent import ChangelogAgent
from app.services.docs.codebase_analyzer import CodebaseAnalyzer
from app.services.docs.document_generator import DocumentGenerator
from app.services.docs.documentation_planner import DocumentationPlanner
from app.services.docs.file_source import GitHubServiceFactory
from app.services.docs.fingerprint import compute_codebase_fingerprint, should_skip_generation
from app.services.docs.plans_agent import PlansAgent
from app.services.docs.types import DocsInfo, OrchestratorResult
from app.services.docs.utils import extract_title, infer_doc_type, map_path_to_folder
from app.services.github import GitHubService
from app.services.github.exceptions import GitHubRepoRenamed
from app.services.github.types import RepoTreeItem

logger = logging.getLogger(__name__)

# Default token budget for codebase analysis
DEFAULT_ANALYSIS_TOKEN_BUDGET = 100_000

# Timeout settings for agent operations (in seconds)
AGENT_TIMEOUT_LIGHT = 60  # For lightweight operations (plans, changelog)
AGENT_TIMEOUT_HEAVY = 300  # For heavy operations (analysis, generation)

T = TypeVar("T")


class DocumentOrchestrator:
    """
    Tier 1 orchestrator that coordinates documentation generation.

    Responsibilities:
    1. Check if repos have existing docs/ folders
    2. Scan and import existing documentation
    3. Analyze codebase deeply (v2)
    4. Plan documentation with Claude Opus 4.5 (v2)
    5. Generate documents sequentially (v2)
    6. Ensure all docs are structured into our opinionated folders
    """

    def __init__(
        self,
        db: AsyncSession,
        product: Product,
        github_service: GitHubService | None = None,
        *,
        github_service_factory: GitHubServiceFactory | None = None,
    ) -> None:
        self.db = db
        self.product = product
        self._github_service = github_service
        self._github_service_factory = github_service_factory

        # V2 services — prefer per-repo factory for codebase analysis
        self.codebase_analyzer = CodebaseAnalyzer(
            github_service=github_service,
            token_budget=DEFAULT_ANALYSIS_TOKEN_BUDGET,
            github_service_factory=github_service_factory,
        )
        self.documentation_planner = DocumentationPlanner()
        self.document_generator = DocumentGenerator(db)

        # V1 sub-agents use the shared GitHubService (fallback for now).
        # Per-repo resolution for sub-agents can be added later.
        if github_service:
            self.changelog_agent = ChangelogAgent(db, product, github_service=github_service)
            self.blueprint_agent = BlueprintAgent(db, product, github_service)
            self.plans_agent = PlansAgent(db, product, github_service)
        else:
            self.changelog_agent = ChangelogAgent(db, product)
            self.blueprint_agent = None  # type: ignore[assignment]
            self.plans_agent = None  # type: ignore[assignment]

    async def _get_github_service(self, repo: Repository) -> GitHubService:
        """Get a GitHubService for a specific repo (per-repo token resolution)."""
        if self._github_service_factory:
            return await self._github_service_factory(repo)
        if self._github_service:
            return self._github_service
        raise ValueError("No GitHubService or factory configured")

    async def run(self, use_v2: bool = True, mode: str = "full") -> OrchestratorResult:
        """
        Main entry point for documentation generation.

        Args:
            use_v2: If True (default), use the v2 flow with deep analysis and planning.
                    If False, use the legacy v1 flow with BlueprintAgent.
            mode: Generation mode:
                  - "full": Regenerate all documentation from scratch (default)
                  - "additive": Only add new docs, preserve existing ones

        V2 Flow:
            1. Scan and import existing docs
            2. Deep codebase analysis (CodebaseAnalyzer)
            3. Plan documentation (DocumentationPlanner with Opus 4.5)
            4. Generate documents sequentially (DocumentGenerator)
            5. Organize plans

        V1 Flow (legacy):
            1. Scan and import existing docs
            2. ChangelogAgent
            3. BlueprintAgent (fixed Overview + Architecture)
            4. PlansAgent

        Returns:
            OrchestratorResult with all processing results
        """
        if use_v2:
            return await self._run_v2(mode=mode)
        else:
            return await self._run_v1()

    async def _run_v2(self, mode: str = "full") -> OrchestratorResult:
        """
        V2 flow: Deep analysis → Intelligent planning → Sequential generation.

        This is the new Documentation Agent v2 flow that uses Claude Opus 4.5
        for planning and generates documents based on actual codebase analysis.

        Note: Import from repositories is now a separate action (not part of generation).
        Generation is purely additive — it only creates AI-generated docs.

        Args:
            mode: "full" to regenerate all docs, "additive" to only add new docs
        """
        results = OrchestratorResult()
        current_fingerprint: str | None = None  # Track codebase fingerprint for skip-if-unchanged

        # CRITICAL: Immediately update progress to establish updated_at timestamp.
        # This ensures stale job detection works even if subsequent operations fail.
        await self._update_progress("starting", "Starting documentation generation...")

        # Map API mode to planner mode (always additive for generated docs)
        planner_mode = "expand" if mode == "additive" else "full"

        logger.info(
            f"Starting v2 documentation orchestration for product {self.product.id} "
            f"(mode: {mode}, planner_mode: {planner_mode})"
        )

        # Get linked repos for codebase analysis
        repos = await self._get_linked_repos()
        logger.info(f"Found {len(repos)} linked repositories")

        # Stage 1: Deep codebase analysis (with timeout)
        await self._update_progress("analyzing", "Analyzing codebase structure...")
        try:
            codebase_context = await self._run_with_timeout(
                self.codebase_analyzer.analyze(repos),
                timeout=AGENT_TIMEOUT_HEAVY,
                stage_name="Codebase analysis",
            )
            logger.info(
                f"Codebase analysis complete: {codebase_context.total_files} files, "
                f"{codebase_context.total_tokens} tokens analyzed"
            )

            # Check fingerprint for skip-if-unchanged optimization
            current_fingerprint = compute_codebase_fingerprint(codebase_context)
            if should_skip_generation(current_fingerprint, self.product.docs_codebase_fingerprint):
                await self._update_progress(
                    "complete", "Documentation up-to-date (codebase unchanged)"
                )
                logger.info(
                    f"Skipping doc generation for product {self.product.id}: "
                    f"codebase unchanged (fingerprint: {current_fingerprint})"
                )
                return results  # Return empty results - docs are already current

        except GitHubRepoRenamed as e:
            # Repository was renamed - find and update the affected repo, then retry once
            for repo in repos:
                if repo.full_name == e.old_full_name:
                    await self._handle_repo_rename(
                        repo, new_full_name=e.new_full_name, repo_id=e.repo_id
                    )
                    break
            # Refresh repos list and retry analysis once
            repos = await self._get_linked_repos()
            try:
                codebase_context = await self._run_with_timeout(
                    self.codebase_analyzer.analyze(repos),
                    timeout=AGENT_TIMEOUT_HEAVY,
                    stage_name="Codebase analysis (retry after rename)",
                )
                logger.info(
                    f"Codebase analysis complete after rename: {codebase_context.total_files} files, "
                    f"{codebase_context.total_tokens} tokens analyzed"
                )
            except Exception as retry_error:
                logger.error(f"Codebase analysis failed after rename: {retry_error}")
                await self._update_progress("error", f"Analysis failed: {retry_error}")
                return await self._run_v1()
        except TimeoutError:
            # Timeout already logged and progress updated, fall back to v1
            return await self._run_v1()
        except Exception as e:
            logger.error(f"Codebase analysis failed: {e}")
            # Fall back to v1 if analysis fails
            await self._update_progress("error", f"Analysis failed: {e}")
            return await self._run_v1()

        # Stage 3: Documentation planning (with timeout)
        await self._update_progress("planning", "Creating documentation plan...")
        try:
            # Get existing docs to avoid duplication
            existing_docs = await self._get_existing_docs()

            planner_result = await self._run_with_timeout(
                self.documentation_planner.create_plan(
                    codebase_context=codebase_context,
                    existing_docs=existing_docs,
                    mode=planner_mode,
                ),
                timeout=AGENT_TIMEOUT_HEAVY,
                stage_name="Documentation planning",
            )

            if not planner_result.success:
                logger.error(f"Documentation planning failed: {planner_result.error}")
                await self._update_progress("error", f"Planning failed: {planner_result.error}")
                return await self._run_v1()

            plan = planner_result.plan
            logger.info(
                f"Documentation plan created: {len(plan.planned_documents)} documents to generate"
            )
        except TimeoutError:
            # Timeout already logged and progress updated, fall back to v1
            return await self._run_v1()
        except Exception as e:
            logger.error(f"Documentation planning failed: {e}")
            await self._update_progress("error", f"Planning failed: {e}")
            return await self._run_v1()

        # Stage 4: Sequential document generation
        if plan.planned_documents:
            total_docs = len(plan.planned_documents)

            async def on_progress(current: int, total: int, title: str) -> None:
                await self._update_progress(
                    "generating",
                    f"Generating {title} ({current}/{total})...",
                )

            try:
                batch_result = await self.document_generator.generate_batch(
                    plan=plan,
                    codebase_context=codebase_context,
                    product=self.product,
                    created_by_user_id=self.product.user_id,
                    on_progress=on_progress,
                )

                results.blueprints.extend(batch_result.documents)
                logger.info(
                    f"Document generation complete: {batch_result.total_generated}/{total_docs} "
                    f"generated, {len(batch_result.failed)} failed"
                )

                if batch_result.failed:
                    logger.warning(f"Failed documents: {batch_result.failed}")

            except Exception as e:
                logger.error(f"Document generation failed: {e}")

        # Postprocessing: changelog + plans (non-critical)
        await self._run_postprocessing_stages(results)

        # Complete - save fingerprint for skip-if-unchanged optimization
        if current_fingerprint:
            await self._save_fingerprint(current_fingerprint)

        await self._update_progress("complete", "Documentation generation complete")

        logger.info(
            f"V2 documentation orchestration complete for product {self.product.id}: "
            f"imported={len(results.imported)}, generated={len(results.blueprints)}"
        )

        return results

    async def _save_fingerprint(self, fingerprint: str) -> None:
        """
        Save codebase fingerprint to product for skip-if-unchanged optimization.

        Uses a fresh session to avoid transaction timeout issues.
        """
        from app.core.database import async_session_maker

        try:
            async with async_session_maker() as session:
                product = await session.get(Product, self.product.id)
                if product:
                    product.docs_codebase_fingerprint = fingerprint
                    await session.commit()
                    self.product.docs_codebase_fingerprint = fingerprint
                    logger.debug(f"Saved codebase fingerprint: {fingerprint}")
        except Exception as e:
            # Log but don't fail - fingerprint is an optimization, not critical
            logger.warning(f"Failed to save fingerprint for product {self.product.id}: {e}")

    async def _run_v1(self) -> OrchestratorResult:
        """
        V1 flow: Legacy flow using BlueprintAgent for fixed Overview + Architecture.

        This is the original documentation flow, kept for backwards compatibility
        and as a fallback if v2 analysis/planning fails.
        """
        results = OrchestratorResult()

        # CRITICAL: Immediately update progress to establish updated_at timestamp.
        # This ensures stale job detection works even if subsequent operations fail.
        await self._update_progress("starting", "Starting documentation generation...")

        logger.info(f"Starting v1 documentation orchestration for product {self.product.id}")

        # Update progress: scanning
        await self._update_progress("scanning", "Scanning repositories for documentation...")

        # Step 1: Check for existing docs/ folder
        repos = await self._get_linked_repos()
        logger.info(f"Found {len(repos)} linked repositories")

        for repo in repos:
            if not repo.full_name:
                continue

            try:
                docs_info = await self._scan_repo_docs(repo)

                if docs_info.has_docs_folder and docs_info.has_markdown_files:
                    logger.info(f"Found {len(docs_info.files)} doc files in {repo.full_name}")
                    # Import existing docs, structure into our folders
                    imported = await self._import_existing_docs(repo, docs_info)
                    results.imported.extend(imported)
            except GitHubRepoRenamed as e:
                # Repository was renamed - update DB and retry once
                updated_repo = await self._handle_repo_rename(
                    repo, new_full_name=e.new_full_name, repo_id=e.repo_id
                )
                if updated_repo:
                    try:
                        docs_info = await self._scan_repo_docs(updated_repo)
                        if docs_info.has_docs_folder and docs_info.has_markdown_files:
                            logger.info(
                                f"Found {len(docs_info.files)} doc files in {updated_repo.full_name}"
                            )
                            imported = await self._import_existing_docs(updated_repo, docs_info)
                            results.imported.extend(imported)
                    except Exception as retry_error:
                        logger.error(
                            f"Failed to scan docs after rename for {updated_repo.full_name}: "
                            f"{retry_error}"
                        )
            except Exception as e:
                logger.error(f"Failed to scan docs for {repo.full_name}: {e}")
                # Continue with other repos

        # Step 2: Delegate to sub-agents (with timeouts)
        # Each agent checks what exists and fills gaps

        # Blueprints (overview, architecture, etc.) - critical for v1
        if self.blueprint_agent:
            await self._update_progress("blueprints", "Generating blueprints...")
            try:
                blueprint_result = await self._run_with_timeout(
                    self.blueprint_agent.run(),
                    timeout=AGENT_TIMEOUT_HEAVY,
                    stage_name="Blueprint generation",
                )
                results.blueprints.extend(blueprint_result.documents)
                logger.info(f"Blueprint result: created {blueprint_result.created_count} new docs")
            except TimeoutError:
                logger.error("Blueprint generation timed out")
            except Exception as e:
                logger.error(f"Blueprint agent failed: {e}")

        # Postprocessing: changelog + plans (non-critical)
        await self._run_postprocessing_stages(results)

        await self._update_progress("complete", "Documentation generation complete")

        logger.info(
            f"V1 documentation orchestration complete for product {self.product.id}: "
            f"imported={len(results.imported)}, blueprints={len(results.blueprints)}"
        )

        return results

    async def _run_postprocessing_stages(self, results: OrchestratorResult) -> None:
        """Run changelog and plans stages (shared between V1 and V2 flows).

        These are non-critical — failures are logged but don't block completion.
        """
        # Changelog
        await self._update_progress("changelog", "Processing changelog...")
        try:
            changelog_result = await self._run_with_timeout(
                self.changelog_agent.run(),
                timeout=AGENT_TIMEOUT_LIGHT,
                stage_name="Changelog processing",
            )
            results.changelog = changelog_result
            logger.info(f"Changelog result: {changelog_result.action}")
        except TimeoutError:
            logger.warning("Changelog processing timed out, continuing...")
        except Exception as e:
            logger.error(f"Changelog agent failed: {e}")

        # Plans organization
        if self.plans_agent:
            await self._update_progress("plans", "Organizing plans...")
            try:
                plans_result = await self._run_with_timeout(
                    self.plans_agent.run(),
                    timeout=AGENT_TIMEOUT_LIGHT,
                    stage_name="Plans organization",
                )
                results.plans_structured = plans_result
                logger.info(f"Plans result: organized {plans_result.organized_count} plans")
            except TimeoutError:
                logger.warning("Plans organization timed out, continuing...")
            except Exception as e:
                logger.error(f"Plans agent failed: {e}")

    async def _get_existing_docs(self) -> list[Document]:
        """Get existing GENERATED documents for this product (for gap analysis).

        Only returns AI-generated docs, not imported ones. This ensures the planner
        only considers Trajan-generated documentation when identifying gaps.
        """
        if self.product.id is None:
            return []
        return await document_ops.get_generated_by_product(self.db, self.product.id)

    async def _get_linked_repos(self) -> list[Repository]:
        """Get all GitHub-linked repositories for this product."""
        if self.product.id is None:
            return []
        return await repository_ops.get_github_repos_by_product(self.db, self.product.id)

    async def _scan_repo_docs(self, repo: Repository) -> DocsInfo:
        """Check if repo has docs/ folder and what's in it."""
        if not repo.full_name:
            return DocsInfo(has_docs_folder=False, has_markdown_files=False, files=[])

        github_service = await self._get_github_service(repo)
        owner, repo_name = repo.full_name.split("/", 1)
        tree = await github_service.get_repo_tree(owner, repo_name, repo.default_branch or "main")

        docs_items: list[RepoTreeItem] = []
        for item in tree.all_items:
            # Include docs/ folder files and root-level changelog
            is_docs_file = item.path.startswith("docs/")
            is_changelog = item.path.lower() in ("changelog.md", "changes.md", "history.md")

            if (is_docs_file or is_changelog) and item.type == "blob":
                docs_items.append(item)

        return DocsInfo(
            has_docs_folder=any(item.path.startswith("docs/") for item in tree.all_items),
            has_markdown_files=any(item.path.endswith(".md") for item in docs_items),
            files=docs_items,
        )

    async def _import_existing_docs(
        self,
        repo: Repository,
        docs_info: DocsInfo,
    ) -> list[Document]:
        """Import existing docs and map to our folder structure."""
        if not repo.full_name:
            return []

        github_service = await self._get_github_service(repo)
        owner, repo_name = repo.full_name.split("/", 1)
        branch = repo.default_branch or "main"
        imported: list[Document] = []

        for item in docs_info.files:
            if not item.path.endswith(".md"):
                continue

            try:
                file_content = await github_service.get_file_content(
                    owner, repo_name, item.path, branch
                )
                if not file_content:
                    continue

                content = file_content.content

                # Map to our folder structure
                folder_path = map_path_to_folder(item.path)
                doc_type = infer_doc_type(item.path, content)

                doc = Document(
                    product_id=self.product.id,
                    created_by_user_id=self.product.user_id,
                    title=extract_title(content, item.path),
                    content=content,
                    type=doc_type,
                    folder={"path": folder_path} if folder_path else None,
                    repository_id=repo.id,
                    is_generated=False,  # Imported from repository, not AI-generated
                )
                self.db.add(doc)
                imported.append(doc)

                logger.info(f"Imported doc: {item.path} -> {folder_path or 'root'}")

            except Exception as e:
                logger.error(f"Failed to import {item.path}: {e}")
                continue

        if imported:
            await self.db.commit()
            # Refresh all imported docs
            for doc in imported:
                await self.db.refresh(doc)

        return imported

    async def _update_progress(self, stage: str, message: str) -> None:
        """Update product's docs_generation_progress for frontend polling.

        Uses a fresh session to avoid Supabase statement timeout issues.
        The transaction pooler (port 6543) has a statement timeout that cancels
        queries if the transaction has been open too long. Since AI operations
        can take minutes, we use a fresh session for each progress update.
        """
        from app.core.database import async_session_maker
        from app.models.product import Product

        progress_data = {
            "stage": stage,
            "message": message,
            "updated_at": datetime.now(UTC).isoformat(),
        }

        try:
            async with async_session_maker() as session:
                # Fetch fresh product instance in new transaction
                product = await session.get(Product, self.product.id)
                if product:
                    product.docs_generation_progress = progress_data
                    await session.commit()
                    # Update local product's progress for consistency
                    self.product.docs_generation_progress = progress_data
        except Exception as e:
            # Log but don't fail the whole operation - progress updates are non-critical
            logger.warning(f"Failed to update progress for product {self.product.id}: {e}")

    async def _handle_repo_rename(
        self,
        repo: Repository,
        new_full_name: str | None = None,
        repo_id: int | None = None,
    ) -> Repository | None:
        """Handle a GitHub repository rename by updating our database record.

        When GitHub returns a 301 redirect, this method updates the Repository
        record with the new name so future requests succeed.

        Args:
            repo: The Repository with the old name
            new_full_name: The new full_name from GitHub's redirect (if available)
            repo_id: GitHub repository ID to resolve if new_full_name is not available

        Returns:
            Updated Repository or None if update failed
        """
        if repo.id is None:
            return None

        old_full_name = repo.full_name

        # If we only have repo_id, resolve it to get the new full_name
        if not new_full_name and repo_id:
            try:
                logger.info(f"Resolving GitHub repo ID {repo_id} to get current name...")
                github_service = await self._get_github_service(repo)
                github_repo = await github_service.get_repo_by_id(repo_id)
                new_full_name = github_repo.full_name
                logger.info(f"Resolved repo ID {repo_id} → {new_full_name}")
            except Exception as e:
                logger.error(f"Failed to resolve repo ID {repo_id}: {e}")
                return None

        if not new_full_name:
            logger.error(f"Cannot update repo {old_full_name}: no new name available")
            return None

        logger.info(f"Repository renamed on GitHub: {old_full_name} → {new_full_name}")

        updated_repo = await repository_ops.update_full_name(self.db, repo.id, new_full_name)
        if updated_repo:
            logger.info(f"Updated repository record: {old_full_name} → {new_full_name}")
            await self.db.commit()
        else:
            logger.error(f"Failed to update repository record for {old_full_name}")

        return updated_repo

    async def _run_with_timeout(
        self,
        coro: Coroutine[Any, Any, T],
        timeout: int,
        stage_name: str,
    ) -> T:
        """
        Run a coroutine with a timeout.

        On timeout, updates progress to error state and raises asyncio.TimeoutError.

        Args:
            coro: The coroutine to run
            timeout: Timeout in seconds
            stage_name: Human-readable name for error messages

        Returns:
            The result of the coroutine

        Raises:
            asyncio.TimeoutError: If the operation times out
        """
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except TimeoutError:
            error_msg = f"{stage_name} timed out after {timeout}s"
            logger.error(error_msg)
            await self._update_progress("error", error_msg)
            raise
