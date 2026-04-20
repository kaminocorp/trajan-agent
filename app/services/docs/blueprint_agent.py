"""
BlueprintAgent - Responsible for blueprints/ folder documentation.

Creates overview and architecture documentation for a project.
Analyzes the project complexity to determine what level of
documentation is needed.
"""

import logging
import uuid as uuid_pkg
from typing import Any, cast

import anthropic
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.rls import set_rls_user_context
from app.domain.document_operations import document_ops
from app.domain.repository_operations import repository_ops
from app.models.document import Document
from app.models.product import Product
from app.services.docs.claude_helpers import MODEL_SONNET, call_with_retry
from app.services.docs.types import BlueprintPlan, BlueprintResult, DocumentSpec
from app.services.github import GitHubService, RepoContext

logger = logging.getLogger(__name__)


class BlueprintAgent:
    """
    Agent responsible for blueprints/ folder.

    Creates overview, architecture, and other introductory documentation.
    Decides what level of detail is needed based on project complexity.
    """

    def __init__(
        self,
        db: AsyncSession,
        product: Product,
        github_service: GitHubService,
        user_id: uuid_pkg.UUID,
    ) -> None:
        self.db = db
        self.product = product
        self.github_service = github_service
        # Acting user — used to re-arm RLS context after each per-doc
        # commit. See ``DocumentGenerator`` for rationale.
        self.user_id = user_id
        self.client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    async def run(self) -> BlueprintResult:
        """
        Analyze project and generate appropriate blueprints.

        Returns:
            BlueprintResult with existing and newly created documents
        """
        existing_blueprints = await self._get_existing_blueprints()
        repo_contexts = await self._fetch_repo_contexts()

        # Determine what's needed based on project complexity
        plan = self._analyze_and_plan(repo_contexts, existing_blueprints)

        if not plan.documents_to_create:
            logger.info(
                f"No new blueprints needed for product {self.product.id}, "
                f"found {len(existing_blueprints)} existing"
            )
            return BlueprintResult(
                documents=existing_blueprints,
                created_count=0,
            )

        # Generate missing blueprints
        created: list[Document] = []
        for spec in plan.documents_to_create:
            try:
                doc = await self._generate_document(spec, repo_contexts)
                created.append(doc)
                logger.info(f"Created blueprint: {spec.title}")
            except Exception as e:
                logger.error(f"Failed to generate blueprint '{spec.title}': {e}")
                # Continue with other blueprints

        return BlueprintResult(
            documents=existing_blueprints + created,
            created_count=len(created),
        )

    async def _get_existing_blueprints(self) -> list[Document]:
        """Get existing blueprint documents."""
        if self.product.id is None:
            return []
        return await document_ops.get_by_folder(self.db, self.product.id, "blueprints")

    async def _fetch_repo_contexts(self) -> list[RepoContext]:
        """Fetch context from all linked repositories."""
        if self.product.id is None:
            return []

        repos = await repository_ops.get_github_repos_by_product(self.db, self.product.id)

        contexts: list[RepoContext] = []
        for repo in repos:
            if repo.full_name:
                try:
                    owner, repo_name = repo.full_name.split("/", 1)
                    ctx = await self.github_service.get_repo_context(
                        owner=owner,
                        repo=repo_name,
                        branch=repo.default_branch,
                        description=repo.description,
                    )
                    contexts.append(ctx)
                except Exception as e:
                    logger.error(f"Failed to fetch context for {repo.full_name}: {e}")

        return contexts

    def _analyze_and_plan(
        self,
        repo_contexts: list[RepoContext],
        existing: list[Document],
    ) -> BlueprintPlan:
        """Determine what documentation is needed."""
        existing_titles = {doc.title.lower() for doc in existing if doc.title}

        documents_to_create: list[DocumentSpec] = []

        # Always need an overview if missing
        if "overview" not in existing_titles and "project overview" not in existing_titles:
            documents_to_create.append(
                DocumentSpec(
                    title="Project Overview",
                    folder_path="blueprints",
                    doc_type="blueprint",
                    prompt_context="Generate a comprehensive project overview",
                )
            )

        # Architecture doc for complex projects
        if self._is_complex_project(repo_contexts) and "architecture" not in existing_titles:
            documents_to_create.append(
                DocumentSpec(
                    title="Architecture",
                    folder_path="blueprints",
                    doc_type="architecture",
                    prompt_context="Generate technical architecture documentation",
                )
            )

        return BlueprintPlan(documents_to_create=documents_to_create)

    def _is_complex_project(self, repo_contexts: list[RepoContext]) -> bool:
        """Determine if project needs detailed architecture docs."""
        if not repo_contexts:
            return False

        total_files = sum(len(ctx.tree.files) if ctx.tree else 0 for ctx in repo_contexts)
        return total_files > 50 or len(repo_contexts) > 1

    async def _generate_document(
        self,
        spec: DocumentSpec,
        repo_contexts: list[RepoContext],
    ) -> Document:
        """Generate a single document using Claude."""
        content = await self._call_claude(spec, repo_contexts)

        doc = Document(
            product_id=self.product.id,
            created_by_user_id=self.product.user_id,
            title=spec.title,
            content=content,
            type=spec.doc_type,
            folder={"path": spec.folder_path},
        )
        self.db.add(doc)
        await self.db.commit()
        # Commit dropped SET LOCAL; re-arm before the refresh SELECT and
        # any downstream sub-agents sharing ``self.db``.
        await set_rls_user_context(self.db, self.user_id)
        await self.db.refresh(doc)
        return doc

    async def _call_claude(
        self,
        spec: DocumentSpec,
        repo_contexts: list[RepoContext],
    ) -> str:
        """Call Claude API to generate documentation content."""
        prompt = self._build_prompt(spec, repo_contexts)
        tool_schema = self._build_tool_schema(spec)

        async def _do_call() -> str:
            response = await self.client.messages.create(
                model=MODEL_SONNET,
                max_tokens=8000,
                tools=cast(Any, [tool_schema]),
                tool_choice=cast(Any, {"type": "tool", "name": "save_document"}),
                messages=[{"role": "user", "content": prompt}],
            )
            return self._parse_response(response, spec)

        return await call_with_retry(_do_call, operation_name="Blueprint generation")

    def _build_prompt(
        self,
        spec: DocumentSpec,
        repo_contexts: list[RepoContext],
    ) -> str:
        """Build the prompt for document generation."""
        sections = [
            f"You are writing documentation for a software project. "
            f"Your task: {spec.prompt_context}",
            "",
            "---",
            "",
            "## Project Information",
            "",
            f"**Name:** {self.product.name}",
            f"**Description:** {self.product.description or 'Not provided'}",
            "",
        ]

        # Add repository information
        for ctx in repo_contexts:
            sections.extend(
                [
                    f"### Repository: {ctx.full_name}",
                    f"**Branch:** {ctx.default_branch}",
                    f"**Description:** {ctx.description or 'Not provided'}",
                    "",
                ]
            )

            # Add languages
            if ctx.languages:
                lang_str = ", ".join(
                    f"{lang.name} ({lang.percentage}%)" for lang in ctx.languages[:5]
                )
                sections.append(f"**Languages:** {lang_str}")
                sections.append("")

            # Add file structure summary
            if ctx.tree:
                sections.append(f"**Files:** {len(ctx.tree.files)} files")
                # Show top-level directories
                top_dirs = set()
                for path in ctx.tree.files[:100]:
                    if "/" in path:
                        top_dirs.add(path.split("/")[0])
                if top_dirs:
                    sections.append(f"**Top-level directories:** {', '.join(sorted(top_dirs))}")
                sections.append("")

            # Add key files content
            if ctx.files:
                sections.append("**Key files:**")
                sections.append("")
                for path, content in ctx.files.items():
                    sections.append(f"**{path}:**")
                    sections.append("```")
                    # Truncate long files
                    if len(content) > 4000:
                        sections.append(content[:4000])
                        sections.append(f"\n... (truncated, {len(content)} chars total)")
                    else:
                        sections.append(content)
                    sections.append("```")
                    sections.append("")

        # Add specific instructions based on document type
        sections.extend(self._get_doc_type_instructions(spec))

        return "\n".join(sections)

    def _get_doc_type_instructions(self, spec: DocumentSpec) -> list[str]:
        """Get specific instructions based on document type."""
        if spec.doc_type == "architecture":
            return [
                "---",
                "",
                "## Instructions",
                "",
                "Generate a technical architecture document covering:",
                "",
                "1. **System Overview** - High-level architecture diagram description",
                "2. **Components** - Major components and their responsibilities",
                "3. **Data Flow** - How data moves through the system",
                "4. **Technology Stack** - Frameworks, libraries, and tools used",
                "5. **Deployment Architecture** - How the system is deployed (if visible)",
                "6. **Integration Points** - External services and APIs",
                "",
                "Use markdown formatting with clear headers and bullet points.",
                "Be specific about actual technologies and patterns used in the codebase.",
            ]
        else:  # Default: project overview / blueprint
            return [
                "---",
                "",
                "## Instructions",
                "",
                "Generate a comprehensive project overview covering:",
                "",
                "1. **Introduction** - What is this project and why does it exist?",
                "2. **Key Features** - Main capabilities and functionality",
                "3. **Getting Started** - How to set up and run the project",
                "4. **Project Structure** - Overview of code organization",
                "5. **Contributing** - How to contribute (if applicable)",
                "",
                "Use markdown formatting with clear headers and bullet points.",
                "Write for a developer who is new to the project.",
            ]

    def _build_tool_schema(self, spec: DocumentSpec) -> dict[str, Any]:
        """Build the tool schema for document generation."""
        return {
            "name": "save_document",
            "description": f"Save the generated {spec.doc_type} document",
            "input_schema": {
                "type": "object",
                "required": ["content"],
                "properties": {
                    "content": {
                        "type": "string",
                        "description": f"The full markdown content of the {spec.doc_type} document",
                    },
                },
            },
        }

    def _parse_response(
        self,
        response: anthropic.types.Message,
        spec: DocumentSpec,
    ) -> str:
        """Parse Claude's response to extract document content."""
        for block in response.content:
            if block.type == "tool_use" and block.name == "save_document":
                data = cast(dict[str, Any], block.input)
                content = data.get("content")
                if isinstance(content, str):
                    return content
                return f"# {spec.title}\n\nContent generation failed."

        logger.warning("Claude did not return a save_document tool use")
        return f"# {spec.title}\n\nContent generation failed."
