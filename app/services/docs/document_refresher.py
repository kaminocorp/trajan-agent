"""
DocumentRefresher - Updates existing documents based on codebase changes.

Part of Documentation Agent v2, Phase 7 (Flow B). This service enables users
to keep their documentation in sync with evolving codebases.

Key capabilities:
- Single document refresh — analyze if doc is still accurate
- Bulk refresh — check all documents for a product
- Smart file detection — identify relevant source files from document content
- Minimal updates — only change what's actually stale
"""

import logging
import re
import uuid as uuid_pkg
from dataclasses import dataclass, field
from typing import Any, cast

import anthropic
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.rls import set_rls_user_context
from app.domain.document_operations import document_ops
from app.models.document import Document
from app.models.repository import Repository
from app.services.docs.claude_helpers import MODEL_SONNET, call_with_retry
from app.services.docs.codebase_analyzer import CodebaseAnalyzer
from app.services.docs.types import CodebaseContext, FileContent
from app.services.github import GitHubService

logger = logging.getLogger(__name__)

# Token limits
MAX_TOKENS_REFRESH = 8000
MAX_CONTEXT_TOKENS = 50000


@dataclass
class RefreshResult:
    """Result of refreshing a single document."""

    document_id: str
    status: str  # "updated", "unchanged", "error"
    changes_summary: str | None = None
    error: str | None = None


@dataclass
class BulkRefreshResult:
    """Result of bulk document refresh."""

    checked: int = 0
    updated: int = 0
    unchanged: int = 0
    errors: int = 0
    details: list[RefreshResult] = field(default_factory=list)


class DocumentRefresher:
    """
    Updates existing documents based on codebase changes.

    Compares document content against current source files and uses Claude
    to determine if updates are needed. If so, generates updated content
    while preserving the document's original intent and structure.
    """

    def __init__(
        self,
        db: AsyncSession,
        github_service: GitHubService,
        user_id: uuid_pkg.UUID,
    ) -> None:
        self.db = db
        self.github_service = github_service
        # Acting user for RLS context — re-armed after every commit on
        # ``self.db`` so the post-commit ``refresh(document)`` SELECT runs
        # under the correct user. ``get_db_with_rls`` sets the initial
        # context; commits drop ``SET LOCAL`` and the listener in
        # ``core/database.py`` rehydrates via ``session.info``.
        self.user_id = user_id
        self.client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
        self.codebase_analyzer = CodebaseAnalyzer(github_service)

    async def refresh_document(
        self,
        document: Document,
        repos: list[Repository],
        codebase_context: CodebaseContext | None = None,
    ) -> RefreshResult:
        """
        Refresh a single document by comparing with current codebase.

        Args:
            document: The document to refresh
            repos: Repositories linked to the product
            codebase_context: Optional pre-computed context (for bulk operations)

        Returns:
            RefreshResult with status and any changes made
        """
        try:
            # Get codebase context if not provided
            if codebase_context is None:
                codebase_context = await self.codebase_analyzer.analyze(repos)

            # Extract relevant files for this document
            relevant_files = self._extract_relevant_files(document, codebase_context)

            if not relevant_files:
                logger.info(f"No relevant files found for '{document.title}', skipping refresh")
                return RefreshResult(
                    document_id=str(document.id),
                    status="unchanged",
                    changes_summary="No relevant source files found to compare against",
                )

            # Ask Claude to review the document
            refresh_response = await self._call_claude(document, relevant_files, codebase_context)

            if refresh_response["needs_update"]:
                # Update the document
                document.content = refresh_response["content"]
                self.db.add(document)
                await self.db.commit()
                # Commit dropped SET LOCAL; re-arm before the refresh SELECT.
                await set_rls_user_context(self.db, self.user_id)
                await self.db.refresh(document)

                logger.info(f"Updated document: {document.title}")
                return RefreshResult(
                    document_id=str(document.id),
                    status="updated",
                    changes_summary=refresh_response["summary"],
                )
            else:
                logger.info(f"Document unchanged: {document.title}")
                return RefreshResult(
                    document_id=str(document.id),
                    status="unchanged",
                    changes_summary="Document is up to date",
                )

        except Exception as e:
            logger.error(f"Failed to refresh document '{document.title}': {e}")
            return RefreshResult(
                document_id=str(document.id),
                status="error",
                error=str(e),
            )

    async def refresh_all(
        self,
        product_id: str,
        repos: list[Repository],
        on_progress: Any | None = None,
    ) -> BulkRefreshResult:
        """
        Refresh all documents for a product.

        Args:
            product_id: The product to refresh docs for
            repos: Repositories linked to the product
            on_progress: Optional callback(current: int, total: int, title: str)

        Returns:
            BulkRefreshResult with summary of all refresh operations
        """
        result = BulkRefreshResult()

        # Get all documents for this product (RLS enforces access)
        documents = await document_ops.get_by_product(self.db, uuid_pkg.UUID(product_id))

        if not documents:
            return result

        # Compute codebase context once for all documents
        try:
            codebase_context = await self.codebase_analyzer.analyze(repos)
        except Exception as e:
            logger.error(f"Failed to analyze codebase for refresh: {e}")
            return BulkRefreshResult(
                checked=0,
                errors=1,
                details=[
                    RefreshResult(
                        document_id="",
                        status="error",
                        error=f"Codebase analysis failed: {e}",
                    )
                ],
            )

        result.checked = len(documents)

        for i, doc in enumerate(documents):
            # Report progress
            if on_progress:
                try:
                    await on_progress(i + 1, len(documents), doc.title or "Untitled")
                except Exception as e:
                    logger.warning(f"Progress callback failed: {e}")

            # Refresh the document
            refresh_result = await self.refresh_document(
                document=doc,
                repos=repos,
                codebase_context=codebase_context,
            )

            result.details.append(refresh_result)

            if refresh_result.status == "updated":
                result.updated += 1
            elif refresh_result.status == "unchanged":
                result.unchanged += 1
            else:
                result.errors += 1

        return result

    def _extract_relevant_files(
        self,
        document: Document,
        context: CodebaseContext,
    ) -> list[FileContent]:
        """
        Extract files relevant to this document from codebase context.

        Uses document content to identify which files should be checked:
        1. Files explicitly mentioned in the document (code blocks, paths)
        2. Files matching the document type (e.g., models for data model docs)
        3. Key files based on document title/purpose
        """
        relevant: list[FileContent] = []
        total_tokens = 0
        content = document.content or ""

        # Build lookup for faster matching
        file_by_path = {f.path: f for f in context.all_key_files}

        # 1. Extract file paths mentioned in the document
        mentioned_paths = self._extract_mentioned_paths(content)
        for path in mentioned_paths:
            if path in file_by_path:
                file = file_by_path[path]
                if total_tokens + file.token_estimate <= MAX_CONTEXT_TOKENS:
                    relevant.append(file)
                    total_tokens += file.token_estimate

        # 2. Match files based on document type
        type_patterns = self._get_type_patterns(document.type)
        for file in context.all_key_files:
            if file in relevant:
                continue
            for pattern in type_patterns:
                if re.search(pattern, file.path):
                    if total_tokens + file.token_estimate <= MAX_CONTEXT_TOKENS:
                        relevant.append(file)
                        total_tokens += file.token_estimate
                    break

        # 3. Add tier 1 files if we have room
        if total_tokens < MAX_CONTEXT_TOKENS // 2:
            for file in context.all_key_files:
                if file in relevant:
                    continue
                if file.tier == 1 and total_tokens + file.token_estimate <= MAX_CONTEXT_TOKENS:
                    relevant.append(file)
                    total_tokens += file.token_estimate

        return relevant

    def _extract_mentioned_paths(self, content: str) -> list[str]:
        """Extract file paths mentioned in document content."""
        paths: list[str] = []

        # Match file paths in code blocks (```path or `path`)
        code_block_pattern = r"```(\S+\.(?:py|ts|tsx|js|jsx|json|md|toml|yaml|yml))"
        paths.extend(re.findall(code_block_pattern, content))

        # Match inline code paths
        inline_pattern = r"`([^`]+\.(?:py|ts|tsx|js|jsx|json|md|toml|yaml|yml))`"
        paths.extend(re.findall(inline_pattern, content))

        # Match explicit path references
        path_pattern = r"(?:backend|frontend|src|app|lib)/[^\s)\"'`]+\.(?:py|ts|tsx|js|jsx)"
        paths.extend(re.findall(path_pattern, content))

        return list(set(paths))

    def _get_type_patterns(self, doc_type: str | None) -> list[str]:
        """Get file patterns relevant to this document type."""
        patterns: dict[str, list[str]] = {
            "architecture": [
                r"main\.py$",
                r"app\.py$",
                r"server\.py$",
                r"index\.tsx?$",
                r"router\.py$",
                r"routes?\.py$",
                r"api/.*\.py$",
            ],
            "blueprint": [
                r"models?\.py$",
                r"schemas?\.py$",
                r"types?\.tsx?$",
                r"config.*\.py$",
            ],
            "note": [],  # Generic, use mentioned paths only
            "plan": [],  # Plans don't need code comparison
            "changelog": [],  # Changelogs are managed separately
        }
        return patterns.get(doc_type or "", [])

    async def _call_claude(
        self,
        document: Document,
        relevant_files: list[FileContent],
        context: CodebaseContext,
    ) -> dict[str, Any]:
        """Call Claude to review and potentially update the document."""
        prompt = self._build_prompt(document, relevant_files, context)
        tool_schema = self._build_tool_schema()

        async def _do_call() -> dict[str, Any]:
            response = await self.client.messages.create(
                model=MODEL_SONNET,
                max_tokens=MAX_TOKENS_REFRESH,
                tools=cast(Any, [tool_schema]),
                tool_choice=cast(Any, {"type": "tool", "name": "save_refresh_result"}),
                messages=[{"role": "user", "content": prompt}],
            )
            return self._parse_response(response)

        return await call_with_retry(_do_call, operation_name="Document refresh")

    def _build_prompt(
        self,
        document: Document,
        relevant_files: list[FileContent],
        context: CodebaseContext,
    ) -> str:
        """Build the prompt for document refresh review."""
        sections = [
            "You are reviewing an existing documentation file for accuracy.",
            "",
            "Your task is to compare this documentation against the current state of the",
            "codebase and determine if any updates are needed.",
            "",
            "---",
            "",
            "## Current Document",
            "",
            f"**Title:** {document.title}",
            f"**Type:** {document.type or 'unknown'}",
            "",
            "**Content:**",
            "",
            "```markdown",
            document.content or "(empty)",
            "```",
            "",
            "---",
            "",
            "## Current Source Files",
            "",
            "Compare the documentation against these source files:",
            "",
        ]

        for file in relevant_files:
            sections.extend(
                [
                    f"### `{file.path}`",
                    "",
                    "```",
                    file.content,
                    "```",
                    "",
                ]
            )

        # Tech stack context
        tech = context.combined_tech_stack
        sections.extend(
            [
                "---",
                "",
                "## Project Context",
                "",
            ]
        )
        if tech.languages:
            sections.append(f"**Languages:** {', '.join(tech.languages)}")
        if tech.frameworks:
            sections.append(f"**Frameworks:** {', '.join(tech.frameworks)}")
        sections.append("")

        # Instructions
        sections.extend(
            [
                "---",
                "",
                "## Review Instructions",
                "",
                "Analyze whether this documentation is still accurate. Consider:",
                "- Are code examples still valid?",
                "- Are described APIs/functions still present and unchanged?",
                "- Are architectural descriptions still accurate?",
                "- Is any information outdated or misleading?",
                "",
                "Use the save_refresh_result tool to indicate your decision:",
                "- If updates are needed: set needs_update=true and provide updated content",
                "- If no updates are needed: set needs_update=false",
                "",
                "When updating, preserve the document's structure and style.",
                "Only change what's actually incorrect or outdated.",
            ]
        )

        return "\n".join(sections)

    def _build_tool_schema(self) -> dict[str, Any]:
        """Build the tool schema for refresh results."""
        return {
            "name": "save_refresh_result",
            "description": "Save the document refresh review result",
            "input_schema": {
                "type": "object",
                "required": ["needs_update", "summary"],
                "properties": {
                    "needs_update": {
                        "type": "boolean",
                        "description": "Whether the document needs to be updated",
                    },
                    "summary": {
                        "type": "string",
                        "description": (
                            "Brief summary of findings. If needs_update is true, describe what "
                            "changed. If false, confirm the document is accurate."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "The updated document content (only required if needs_update is true). "
                            "Must be complete markdown content preserving the document structure."
                        ),
                    },
                },
            },
        }

    def _parse_response(self, response: anthropic.types.Message) -> dict[str, Any]:
        """Parse Claude's response to extract refresh result."""
        for block in response.content:
            if block.type == "tool_use" and block.name == "save_refresh_result":
                data = cast(dict[str, Any], block.input)
                return {
                    "needs_update": data.get("needs_update", False),
                    "summary": data.get("summary", ""),
                    "content": data.get("content", ""),
                }

        logger.warning("Claude did not return a save_refresh_result tool use")
        return {"needs_update": False, "summary": "Failed to parse response", "content": ""}
