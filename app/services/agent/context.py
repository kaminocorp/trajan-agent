"""Context builder for the CLI Agent.

Assembles project context from existing domain operations
into a formatted string for the agent's system prompt.
"""

import hashlib
import logging
import uuid as uuid_pkg
from collections.abc import Sequence
from typing import Any

from cachetools import TTLCache  # type: ignore[import-untyped]
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import (
    document_ops,
    product_ops,
    progress_summary_ops,
    repository_ops,
    work_item_ops,
)
from app.services.file_selector import FileSelector, FileSelectorInput
from app.services.github import GitHubService
from app.services.github.cache import agent_context_cache

logger = logging.getLogger(__name__)

# Max chars for the entire GitHub context section
_GITHUB_CONTEXT_CHAR_LIMIT = 2000
_GITHUB_MAX_REPOS = 3

# Codebase key-files cache: 5-minute TTL, keyed by product_id
_CODEBASE_CONTEXT_CHAR_LIMIT = 15_000
_codebase_cache: TTLCache[str, str | None] = TTLCache(maxsize=100, ttl=300)

# AI-selected source code cache: 10-minute TTL, keyed by product_id
_SOURCE_CODE_CONTEXT_CHAR_LIMIT = 200_000  # ~50K tokens
_source_code_cache: TTLCache[str, str | None] = TTLCache(maxsize=50, ttl=600)


class ContextBuilder:
    """Builds context string for the agent from project data."""

    async def build(
        self,
        db: AsyncSession,
        product_id: uuid_pkg.UUID,
        github_token: str | None = None,
    ) -> str:
        """Fetch and format all relevant project context into a string."""
        sections: list[str] = []

        # Product info
        product = await product_ops.get(db, product_id)
        if product:
            sections.append(self._format_product(product))

        # Repositories
        repos = await repository_ops.get_by_product(db, product_id, limit=50)
        if repos:
            sections.append(self._format_repositories(repos))

        # Work items
        items = await work_item_ops.get_by_product(db, product_id, limit=50)
        if items:
            sections.append(self._format_work_items(items))

        # Documents
        docs = await document_ops.get_by_product(db, product_id, limit=50)
        if docs:
            sections.append(self._format_documents(docs))

        # Progress summary (last 7 days)
        summary = await progress_summary_ops.get_by_product_period(db, product_id, "7d")
        if summary:
            sections.append(self._format_progress(summary))

        # Live GitHub activity
        if github_token and repos:
            gh_context = await self._fetch_github_context(github_token, repos)
            if gh_context:
                sections.append(gh_context)

        # Codebase key files (cached 5 min)
        if github_token and repos:
            codebase_ctx = await self._build_codebase_section(github_token, product_id, repos)
            if codebase_ctx:
                sections.append(codebase_ctx)

        # AI-selected source code files (cached 10 min)
        if github_token and repos:
            source_ctx = await self._build_source_code_section(github_token, product_id, repos)
            if source_ctx:
                sections.append(source_ctx)

        return "\n\n".join(sections) if sections else "No project data available."

    async def _fetch_github_context(
        self,
        github_token: str,
        repos: Sequence[object],
    ) -> str | None:
        """Fetch live GitHub activity for the product's repos.

        Returns a formatted context section, or None if all calls fail.
        Gracefully handles token revocation, rate limits, and access errors.
        Results are cached for 60s to avoid hammering GitHub during rapid chat.
        """
        # Build cache key from repo names (token not included — same repos = same data)
        repo_names = sorted(getattr(r, "full_name", "") for r in repos[:_GITHUB_MAX_REPOS])
        cache_key = hashlib.md5(f"agent_ctx:{':'.join(repo_names)}".encode()).hexdigest()

        cached: str | None = agent_context_cache.get(cache_key)
        if cached is not None:
            return cached

        gh = GitHubService(github_token)
        repo_sections: list[str] = []

        for repo in repos[:_GITHUB_MAX_REPOS]:
            full_name = getattr(repo, "full_name", None)
            if not full_name or "/" not in full_name:
                continue
            owner, name = full_name.split("/", 1)

            try:
                section = await self._fetch_single_repo_context(gh, owner, name)
                if section:
                    repo_sections.append(section)
            except Exception:
                logger.warning("GitHub context fetch failed for %s", full_name, exc_info=True)
                continue

        if not repo_sections:
            return None

        result = "## GitHub Activity (Live)\n" + "\n".join(repo_sections)
        result = result[:_GITHUB_CONTEXT_CHAR_LIMIT]
        agent_context_cache[cache_key] = result
        return result

    async def _build_codebase_section(
        self,
        github_token: str,
        product_id: uuid_pkg.UUID,
        repos: Sequence[object],
    ) -> str | None:
        """Fetch key infrastructure files from connected repos.

        Uses GitHubService.get_key_files() to retrieve README, package.json,
        Dockerfile, etc. Results are cached for 5 minutes per product to avoid
        re-fetching on every chat message within a session.

        Returns a formatted context section, or None if no files were retrieved.
        """
        cache_key = str(product_id)
        cached: str | None = _codebase_cache.get(cache_key)
        if cached is not None:
            return cached if cached else None

        gh = GitHubService(github_token)
        all_files: dict[str, dict[str, str]] = {}  # repo_name -> {path: content}

        for repo in repos[:_GITHUB_MAX_REPOS]:
            full_name = getattr(repo, "full_name", None)
            if not full_name or "/" not in full_name:
                continue
            owner, name = full_name.split("/", 1)
            default_branch = getattr(repo, "default_branch", None) or "main"

            try:
                files = await gh.get_key_files(owner, name, branch=default_branch)
                if files:
                    all_files[full_name] = files
            except Exception:
                logger.warning("Codebase key-files fetch failed for %s", full_name, exc_info=True)
                continue

        if not all_files:
            _codebase_cache[cache_key] = ""
            return None

        # Format into a context section
        lines: list[str] = ["## Codebase Key Files"]
        total_chars = 0

        for repo_name, files in all_files.items():
            lines.append(f"\n### {repo_name}")
            for path, content in files.items():
                header = f"\n**{path}**\n```\n"
                footer = "\n```"
                remaining = _CODEBASE_CONTEXT_CHAR_LIMIT - total_chars - len(header) - len(footer)
                if remaining <= 0:
                    break
                truncated = content[:remaining]
                block = f"{header}{truncated}{footer}"
                lines.append(block)
                total_chars += len(block)
            if total_chars >= _CODEBASE_CONTEXT_CHAR_LIMIT:
                break

        result = "\n".join(lines)
        _codebase_cache[cache_key] = result
        return result

    async def _build_source_code_section(
        self,
        github_token: str,
        product_id: uuid_pkg.UUID,
        repos: Sequence[object],
    ) -> str | None:
        """Fetch AI-selected architecture files from connected repos.

        Pipeline per repo:
        1. get_repo_tree() — discover all files
        2. FileSelector.select_files() — AI picks 10-50 significant files
        3. fetch_files_by_paths() — fetch selected file contents

        Results are cached for 10 minutes per product. First message in a
        session pays the latency cost (~3-5s); subsequent messages hit cache.

        Returns a formatted context section, or None if no files were retrieved.
        """
        cache_key = f"src:{product_id}"
        cached: str | None = _source_code_cache.get(cache_key)
        if cached is not None:
            return cached if cached else None

        gh = GitHubService(github_token)
        file_selector = FileSelector()
        all_source: dict[str, dict[str, str]] = {}  # repo_name -> {path: content}

        for repo in repos[:_GITHUB_MAX_REPOS]:
            full_name = getattr(repo, "full_name", None)
            if not full_name or "/" not in full_name:
                continue
            owner, name = full_name.split("/", 1)
            default_branch = getattr(repo, "default_branch", None) or "main"
            description = getattr(repo, "description", None)

            try:
                # 1. Get file tree
                tree = await gh.get_repo_tree(owner, name, branch=default_branch)
                if not tree or not tree.files:
                    continue

                # 2. Get README for FileSelector context (check common names)
                readme_content: str | None = None
                for readme_name in ("README.md", "readme.md", "README"):
                    if readme_name in tree.files:
                        readme_file = await gh.get_file_content(
                            owner, name, readme_name, default_branch
                        )
                        if readme_file:
                            readme_content = readme_file.content
                            break

                # 3. AI file selection
                selector_input = FileSelectorInput(
                    repo_name=full_name,
                    description=description,
                    readme_content=readme_content,
                    file_paths=tree.files,
                )
                selection = await file_selector.select_files(selector_input)

                if not selection.selected_files:
                    continue

                # 4. Fetch selected files
                files = await gh.fetch_files_by_paths(
                    owner, name, selection.selected_files, branch=default_branch
                )
                if files:
                    all_source[full_name] = files
                    logger.info(
                        "Source code context: fetched %d/%d files for %s%s",
                        len(files),
                        len(selection.selected_files),
                        full_name,
                        " (fallback)" if selection.used_fallback else "",
                    )
            except Exception:
                logger.warning(
                    "Source code context fetch failed for %s",
                    full_name,
                    exc_info=True,
                )
                continue

        if not all_source:
            _source_code_cache[cache_key] = ""
            return None

        # Format into a context section
        lines: list[str] = ["## Source Code"]
        total_chars = 0

        for repo_name, files in all_source.items():
            lines.append(f"\n### {repo_name}")
            for path, content in files.items():
                header = f"\n**{path}**\n```\n"
                footer = "\n```"
                remaining = (
                    _SOURCE_CODE_CONTEXT_CHAR_LIMIT - total_chars - len(header) - len(footer)
                )
                if remaining <= 0:
                    break
                truncated = content[:remaining]
                block = f"{header}{truncated}{footer}"
                lines.append(block)
                total_chars += len(block)
            if total_chars >= _SOURCE_CODE_CONTEXT_CHAR_LIMIT:
                break

        result = "\n".join(lines)
        _source_code_cache[cache_key] = result
        return result

    @staticmethod
    async def _fetch_single_repo_context(
        gh: GitHubService,
        owner: str,
        name: str,
    ) -> str | None:
        """Fetch commits, PRs, and issues for a single repo."""
        lines: list[str] = [f"### {owner}/{name}"]
        has_data = False

        # Recent commits
        commits: list[dict[str, Any]] = await gh.get_recent_commits(owner, name, per_page=5)
        if commits:
            has_data = True
            lines.append("Recent commits:")
            for c in commits:
                msg = c["message"][:72]
                lines.append(f"  - {c['sha']} {msg} ({c['author']})")

        # Open PRs
        pulls: list[dict[str, Any]] = await gh.get_open_pulls(owner, name, per_page=5)
        if pulls:
            has_data = True
            lines.append("Open PRs:")
            for pr in pulls:
                title = pr["title"][:60]
                lines.append(f"  - #{pr['number']} {title} (by {pr['author']})")

        # Open issues
        issues: list[dict[str, Any]] = await gh.get_open_issues(owner, name, per_page=5)
        if issues:
            has_data = True
            lines.append("Open issues:")
            for issue in issues:
                title = issue["title"][:60]
                labels = ", ".join(issue["labels"][:3])
                line = f"  - #{issue['number']} {title}"
                if labels:
                    line += f" [{labels}]"
                lines.append(line)

        return "\n".join(lines) if has_data else None

    async def build_summary(
        self,
        db: AsyncSession,
        product_id: uuid_pkg.UUID,
        github_token: str | None = None,
    ) -> dict[str, Any]:
        """Build a structured summary of what the agent can access."""
        product = await product_ops.get(db, product_id)
        repos = await repository_ops.get_by_product(db, product_id, limit=50)
        items = await work_item_ops.get_by_product(db, product_id, limit=50)
        docs = await document_ops.get_by_product(db, product_id, limit=50)
        summary = await progress_summary_ops.get_by_product_period(db, product_id, "7d")

        has_github = bool(github_token and repos)
        repo_names = [
            getattr(r, "full_name", "")
            for r in (repos or [])[:_GITHUB_MAX_REPOS]
            if getattr(r, "full_name", "")
        ]

        return {
            "project": {
                "name": {"accessible": bool(product), "value": getattr(product, "name", None)},
                "description": {
                    "accessible": bool(product and getattr(product, "description", None))
                },
                "overview": {
                    "accessible": bool(product and getattr(product, "product_overview", None))
                },
                "repositories": {"accessible": bool(repos), "count": len(repos or [])},
                "work_items": {"accessible": bool(items), "count": len(items or [])},
                "documents": {"accessible": bool(docs), "count": len(docs or [])},
                "progress_summary": {"accessible": bool(summary), "window_days": 7},
                "environment_variables": {
                    "accessible": False,
                    "reason": "Excluded for security",
                },
                "app_info": {"accessible": False, "reason": "Not included in agent context"},
                "codebase_key_files": {
                    "accessible": has_github,
                    "files": "README, package.json, pyproject.toml, Dockerfile, etc.",
                    "cache_ttl_seconds": 300,
                },
                "source_code": {
                    "accessible": has_github,
                    "description": "AI-selected architecture files (routes, models, services, etc.)",
                    "max_files_per_repo": 50,
                    "cache_ttl_seconds": 600,
                },
            },
            "github": {
                "connected": has_github,
                "capabilities": {
                    "recent_commits": {"per_repo": 5, "max_repos": _GITHUB_MAX_REPOS},
                    "open_pull_requests": {"per_repo": 5, "max_repos": _GITHUB_MAX_REPOS},
                    "open_issues": {"per_repo": 5, "max_repos": _GITHUB_MAX_REPOS},
                },
                "repos_in_scope": repo_names if has_github else [],
            },
        }

    @staticmethod
    def _format_product(product: object) -> str:
        """Format product info section."""
        lines = ["## Product"]
        lines.append(f"Name: {getattr(product, 'name', 'Unknown')}")
        desc = getattr(product, "description", None)
        if desc:
            lines.append(f"Description: {desc}")
        overview = getattr(product, "product_overview", None)
        if overview:
            text = str(overview) if not isinstance(overview, str) else overview
            lines.append(f"Overview: {text[:500]}")
        return "\n".join(lines)

    @staticmethod
    def _format_repositories(repos: Sequence[object]) -> str:
        """Format repositories section."""
        lines = [f"## Repositories ({len(repos)})"]
        for repo in repos:
            name = getattr(repo, "full_name", None) or getattr(repo, "name", "Unknown")
            lang = getattr(repo, "language", None) or "Unknown"
            desc = getattr(repo, "description", None) or ""
            stars = getattr(repo, "stars_count", 0) or 0
            line = f"- {name} [{lang}]"
            if stars:
                line += f" ★{stars}"
            if desc:
                line += f" — {desc[:80]}"
            lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _format_work_items(items: Sequence[object]) -> str:
        """Format work items section."""
        lines = [f"## Work Items ({len(items)})"]
        for item in items:
            title = getattr(item, "title", "Untitled")
            item_type = getattr(item, "type", "task")
            status = getattr(item, "status", "unknown")
            priority = getattr(item, "priority", None)
            line = f"- [{status}] {title} ({item_type})"
            if priority:
                line += f" priority={priority}"
            lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _format_documents(docs: Sequence[object]) -> str:
        """Format documents section."""
        lines = [f"## Documents ({len(docs)})"]
        for doc in docs:
            title = getattr(doc, "title", "Untitled")
            doc_type = getattr(doc, "type", "document")
            pinned = getattr(doc, "is_pinned", False)
            line = f"- {title} ({doc_type})"
            if pinned:
                line += " [pinned]"
            lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _format_progress(summary: object) -> str:
        """Format progress summary section."""
        lines = ["## Recent Activity (Last 7 Days)"]
        text = getattr(summary, "summary_text", None)
        if text:
            lines.append(text[:500])
        commits = getattr(summary, "total_commits", 0)
        contributors = getattr(summary, "total_contributors", 0)
        if commits or contributors:
            lines.append(f"Stats: {commits} commits, {contributors} contributors")
        return "\n".join(lines)
