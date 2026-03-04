"""
CodebaseAnalyzer - Deep codebase analysis for documentation generation.

Part of Documentation Agent v2. This service performs thorough analysis
of repository contents to build rich context for the DocumentationPlanner.
"""

import logging
import re

from app.models.repository import Repository
from app.services.docs.codebase_analyzer.constants import (
    CHARS_PER_TOKEN,
    DEFAULT_TOKEN_BUDGET,
    MAX_FILE_SIZE,
    SKIP_PATTERNS,
    TIER_1_PATTERNS,
    TIER_2_PATTERNS,
    TIER_3_PATTERNS,
)
from app.services.docs.codebase_analyzer.endpoints import extract_endpoints
from app.services.docs.codebase_analyzer.models import extract_models
from app.services.docs.codebase_analyzer.patterns import detect_patterns
from app.services.docs.codebase_analyzer.tech_stack import detect_tech_stack
from app.services.docs.file_source import GitHubServiceFactory
from app.services.docs.types import (
    CodebaseContext,
    EndpointInfo,
    FileContent,
    ModelInfo,
    RepoAnalysis,
    TechStack,
)
from app.services.github import GitHubService
from app.services.github.exceptions import GitHubRepoRenamed
from app.services.github.types import RepoTree

logger = logging.getLogger(__name__)


class CodebaseAnalyzer:
    """
    Analyzes codebase content to build rich context for documentation.

    Fetches and reads key source files from repositories, identifies
    frameworks, data models, API endpoints, and architectural patterns.
    """

    def __init__(
        self,
        github_service: GitHubService | None = None,
        token_budget: int = DEFAULT_TOKEN_BUDGET,
        *,
        github_service_factory: GitHubServiceFactory | None = None,
    ) -> None:
        self._github_service = github_service
        self._github_service_factory = github_service_factory
        self.token_budget = token_budget

    async def _get_github_service(self, repo: Repository) -> GitHubService:
        """Get a GitHubService for a specific repo (per-repo token resolution)."""
        if self._github_service_factory:
            return await self._github_service_factory(repo)
        if self._github_service:
            return self._github_service
        raise ValueError("No GitHubService or factory configured")

    async def analyze(self, repos: list[Repository]) -> CodebaseContext:
        """
        Perform deep analysis of all repositories.

        Args:
            repos: List of Repository models to analyze

        Returns:
            CodebaseContext with comprehensive analysis results
        """
        all_analyses: list[RepoAnalysis] = []
        all_errors: list[str] = []
        total_tokens = 0

        # Distribute token budget across repos (with minimum per repo)
        per_repo_budget = max(self.token_budget // max(len(repos), 1), 20_000)

        for repo in repos:
            if not repo.full_name:
                all_errors.append(f"Repository {repo.name} has no full_name, skipping")
                continue

            try:
                analysis = await self._analyze_repo(repo, per_repo_budget)
                all_analyses.append(analysis)
                total_tokens += sum(f.token_estimate for f in analysis.key_files)
            except GitHubRepoRenamed:
                # Let rename exceptions bubble up so orchestrator can handle them
                raise
            except Exception as e:
                error_msg = f"Failed to analyze {repo.full_name}: {e}"
                logger.error(error_msg)
                all_errors.append(error_msg)

        # Combine results across all repos
        return self._combine_analyses(all_analyses, total_tokens, all_errors)

    async def _analyze_repo(
        self,
        repo: Repository,
        token_budget: int,
    ) -> RepoAnalysis:
        """Analyze a single repository."""
        assert repo.full_name is not None
        owner, repo_name = repo.full_name.split("/", 1)
        branch = repo.default_branch or "main"

        errors: list[str] = []

        # Resolve per-repo GitHub service
        github_service = await self._get_github_service(repo)

        # Fetch file tree
        try:
            tree = await github_service.get_repo_tree(owner, repo_name, branch)
        except Exception as e:
            logger.error(f"Failed to get tree for {repo.full_name}: {e}")
            return RepoAnalysis(
                full_name=repo.full_name,
                default_branch=branch,
                description=repo.description,
                tech_stack=TechStack([], [], [], [], []),
                key_files=[],
                models=[],
                endpoints=[],
                detected_patterns=[],
                total_files=0,
                errors=[str(e)],
            )

        # Select and fetch files with priority tiers
        key_files = await self._fetch_prioritized_files(
            github_service, owner, repo_name, branch, tree, token_budget
        )

        # Detect tech stack from file contents
        tech_stack = detect_tech_stack(key_files, tree)

        # Extract models and endpoints
        models = extract_models(key_files)
        endpoints = extract_endpoints(key_files)

        # Detect patterns
        patterns = detect_patterns(tree, tech_stack)

        return RepoAnalysis(
            full_name=repo.full_name,
            default_branch=branch,
            description=repo.description,
            tech_stack=tech_stack,
            key_files=key_files,
            models=models,
            endpoints=endpoints,
            detected_patterns=patterns,
            total_files=len(tree.files),
            errors=errors,
        )

    async def _fetch_prioritized_files(
        self,
        github_service: GitHubService,
        owner: str,
        repo: str,
        branch: str,
        tree: RepoTree,
        token_budget: int,
    ) -> list[FileContent]:
        """
        Fetch files using priority tiers with token budget management.

        Tier 1 files are always fetched, Tier 2 if budget allows,
        Tier 3 only summarized (not content).
        """
        # Classify files by tier
        tier_1_files: list[str] = []
        tier_2_files: list[str] = []
        tier_3_files: list[str] = []

        for file_path in tree.files:
            if self._should_skip(file_path):
                continue

            tier = self._get_file_tier(file_path)
            if tier == 1:
                tier_1_files.append(file_path)
            elif tier == 2:
                tier_2_files.append(file_path)
            elif tier == 3:
                tier_3_files.append(file_path)

        result: list[FileContent] = []
        remaining_budget = token_budget

        # Fetch Tier 1 (always)
        t1_contents = await github_service.fetch_files_by_paths(
            owner, repo, tier_1_files, branch, max_size=MAX_FILE_SIZE
        )
        for path, content in t1_contents.items():
            tokens = len(content) // CHARS_PER_TOKEN
            result.append(
                FileContent(
                    path=path,
                    content=content,
                    size=len(content),
                    tier=1,
                    token_estimate=tokens,
                )
            )
            remaining_budget -= tokens

        # Fetch Tier 2 (if budget allows)
        if remaining_budget > 0 and tier_2_files:
            # Limit number of tier 2 files based on remaining budget
            # Estimate ~500 tokens per file average
            max_tier_2 = min(len(tier_2_files), remaining_budget // 500)
            t2_to_fetch = tier_2_files[:max_tier_2]

            t2_contents = await github_service.fetch_files_by_paths(
                owner, repo, t2_to_fetch, branch, max_size=MAX_FILE_SIZE
            )
            for path, content in t2_contents.items():
                tokens = len(content) // CHARS_PER_TOKEN
                if tokens <= remaining_budget:
                    result.append(
                        FileContent(
                            path=path,
                            content=content,
                            size=len(content),
                            tier=2,
                            token_estimate=tokens,
                        )
                    )
                    remaining_budget -= tokens

        logger.info(
            f"Fetched {len(result)} files for {owner}/{repo}: "
            f"{len([f for f in result if f.tier == 1])} tier 1, "
            f"{len([f for f in result if f.tier == 2])} tier 2, "
            f"tokens used: {token_budget - remaining_budget}"
        )

        return result

    def _should_skip(self, path: str) -> bool:
        """Check if file should be skipped entirely."""
        return any(re.match(pattern, path, re.IGNORECASE) for pattern in SKIP_PATTERNS)

    def _get_file_tier(self, path: str) -> int:
        """Determine priority tier for a file (1=highest, 3=lowest, 0=skip)."""
        for pattern in TIER_1_PATTERNS:
            if re.match(pattern, path, re.IGNORECASE):
                return 1

        for pattern in TIER_2_PATTERNS:
            if re.match(pattern, path, re.IGNORECASE):
                return 2

        for pattern in TIER_3_PATTERNS:
            if re.match(pattern, path, re.IGNORECASE):
                return 3

        # Default: skip files not matching any pattern
        return 0

    def _combine_analyses(
        self,
        analyses: list[RepoAnalysis],
        total_tokens: int,
        errors: list[str],
    ) -> CodebaseContext:
        """Combine analyses from multiple repositories."""
        if not analyses:
            return CodebaseContext(
                repositories=[],
                combined_tech_stack=TechStack([], [], [], [], []),
                all_key_files=[],
                all_models=[],
                all_endpoints=[],
                detected_patterns=[],
                total_files=0,
                total_tokens=0,
                errors=errors,
            )

        # Merge tech stacks
        all_languages: set[str] = set()
        all_frameworks: set[str] = set()
        all_databases: set[str] = set()
        all_infra: set[str] = set()
        all_pkg_managers: set[str] = set()

        all_key_files: list[FileContent] = []
        all_models: list[ModelInfo] = []
        all_endpoints: list[EndpointInfo] = []
        all_patterns: set[str] = set()
        total_files = 0

        for analysis in analyses:
            all_languages.update(analysis.tech_stack.languages)
            all_frameworks.update(analysis.tech_stack.frameworks)
            all_databases.update(analysis.tech_stack.databases)
            all_infra.update(analysis.tech_stack.infrastructure)
            all_pkg_managers.update(analysis.tech_stack.package_managers)

            all_key_files.extend(analysis.key_files)
            all_models.extend(analysis.models)
            all_endpoints.extend(analysis.endpoints)
            all_patterns.update(analysis.detected_patterns)
            total_files += analysis.total_files
            errors.extend(analysis.errors)

        combined_tech_stack = TechStack(
            languages=sorted(all_languages),
            frameworks=sorted(all_frameworks),
            databases=sorted(all_databases),
            infrastructure=sorted(all_infra),
            package_managers=sorted(all_pkg_managers),
        )

        return CodebaseContext(
            repositories=analyses,
            combined_tech_stack=combined_tech_stack,
            all_key_files=all_key_files,
            all_models=all_models,
            all_endpoints=all_endpoints,
            detected_patterns=sorted(all_patterns),
            total_files=total_files,
            total_tokens=total_tokens,
            errors=errors,
        )
