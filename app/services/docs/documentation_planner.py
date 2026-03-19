"""
DocumentationPlanner - Uses Claude Opus 4.5 to create documentation plans.

This is the "brain" of Documentation Agent v2. It analyzes the codebase context
(from CodebaseAnalyzer) and existing documentation, then autonomously decides
what documentation should be created.

Quality and thoughtfulness matter more than speed. The planner should:
1. Thoroughly understand the codebase
2. Consider both business and technical stakeholders
3. Identify gaps in existing documentation
4. Create a prioritized plan of documents to generate
"""

import logging
from typing import Any, cast

import anthropic

from app.config import settings
from app.models.document import Document
from app.services.docs.claude_helpers import MODEL_OPUS, call_with_retry
from app.services.docs.section_config import (
    VALID_SECTIONS,
    VALID_SUBSECTIONS,
    get_subsection_prompt,
)
from app.services.docs.types import (
    CodebaseContext,
    DocumentationPlan,
    PlannedDocument,
    PlannerResult,
)

logger = logging.getLogger(__name__)

# Document type categories for the planner
DOC_TYPES = [
    "overview",  # Project introduction, what it does
    "architecture",  # Technical architecture, system design
    "guide",  # How-to guides, tutorials
    "reference",  # API reference, configuration reference
    "concept",  # Core concepts, mental models
]

# Folder mapping for document types
DOC_TYPE_FOLDERS = {
    "overview": "blueprints",
    "architecture": "blueprints",
    "guide": "blueprints",
    "reference": "blueprints",
    "concept": "blueprints",
}


class DocumentationPlanner:
    """
    Uses Claude Opus 4.5 to create a documentation plan.

    Takes codebase analysis and existing docs as input, returns a prioritized
    plan of documents to generate. The planner makes autonomous decisions about
    what documentation would be most valuable.
    """

    def __init__(self) -> None:
        self.client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    async def create_plan(
        self,
        codebase_context: CodebaseContext,
        existing_docs: list[Document],
        mode: str = "full",
    ) -> PlannerResult:
        """
        Create a documentation plan based on codebase analysis.

        Args:
            codebase_context: Deep analysis of the codebase (from CodebaseAnalyzer)
            existing_docs: List of existing documents to avoid duplicating
            mode: Planning mode - "full" for complete suite, "expand" for gap-only

        Returns:
            PlannerResult containing the documentation plan
        """
        try:
            plan = await self._call_claude(codebase_context, existing_docs, mode)
            return PlannerResult(plan=plan, success=True)
        except Exception as e:
            logger.error(f"Documentation planning failed: {e}")
            # Return empty plan on failure
            return PlannerResult(
                plan=DocumentationPlan(
                    summary=f"Planning failed: {e}",
                    planned_documents=[],
                    skipped_existing=[],
                    codebase_summary="",
                ),
                success=False,
                error=str(e),
            )

    async def _call_claude(
        self,
        context: CodebaseContext,
        existing_docs: list[Document],
        mode: str,
    ) -> DocumentationPlan:
        """Call Claude API to generate the documentation plan."""
        prompt = self._build_prompt(context, existing_docs, mode)
        tool_schema = self._build_tool_schema()

        async def _do_call() -> DocumentationPlan:
            response = await self.client.messages.create(
                model=MODEL_OPUS,
                max_tokens=8000,
                tools=cast(Any, [tool_schema]),
                tool_choice=cast(Any, {"type": "tool", "name": "save_documentation_plan"}),
                messages=[{"role": "user", "content": prompt}],
            )
            return self._parse_response(response)

        return await call_with_retry(_do_call, operation_name="Documentation planning")

    def _build_prompt(
        self,
        context: CodebaseContext,
        existing_docs: list[Document],
        mode: str,
    ) -> str:
        """Build the prompt for documentation planning."""
        sections = [
            "You are a technical documentation expert. Your task is to analyze this codebase "
            "and create a documentation plan.",
            "",
            "---",
            "",
        ]

        # Mode-specific instructions
        if mode == "expand":
            sections.extend(
                [
                    "## Mode: Expand (Gap Analysis Only)",
                    "",
                    "You are in EXPAND mode. This means:",
                    "- Focus ONLY on identifying gaps in existing documentation",
                    "- Do NOT suggest documents that would duplicate existing content",
                    "- Existing docs are to be left untouched",
                    "- Only recommend new documents that fill genuine gaps",
                    "- It's valid to recommend zero documents if coverage is comprehensive",
                    "",
                    "---",
                    "",
                ]
            )

        # Codebase context section
        sections.extend(
            [
                "## Codebase Analysis",
                "",
                f"**Repositories:** {len(context.repositories)}",
                f"**Total Files:** {context.total_files}",
                f"**Total Tokens Analyzed:** {context.total_tokens:,}",
                "",
            ]
        )

        # Tech stack
        tech = context.combined_tech_stack
        if tech.languages:
            sections.append(f"**Languages:** {', '.join(tech.languages)}")
        if tech.frameworks:
            sections.append(f"**Frameworks:** {', '.join(tech.frameworks)}")
        if tech.databases:
            sections.append(f"**Databases:** {', '.join(tech.databases)}")
        if tech.infrastructure:
            sections.append(f"**Infrastructure:** {', '.join(tech.infrastructure)}")
        sections.append("")

        # Detected patterns
        if context.detected_patterns:
            sections.append(f"**Architecture Patterns:** {', '.join(context.detected_patterns)}")
            sections.append("")

        # Data models
        if context.all_models:
            sections.append(f"**Data Models:** {len(context.all_models)} detected")
            model_summary = ", ".join(m.name for m in context.all_models[:10])
            if len(context.all_models) > 10:
                model_summary += f" (+{len(context.all_models) - 10} more)"
            sections.append(f"  Examples: {model_summary}")
            sections.append("")

        # API endpoints
        if context.all_endpoints:
            sections.append(f"**API Endpoints:** {len(context.all_endpoints)} detected")
            endpoint_summary = ", ".join(f"{e.method} {e.path}" for e in context.all_endpoints[:8])
            if len(context.all_endpoints) > 8:
                endpoint_summary += f" (+{len(context.all_endpoints) - 8} more)"
            sections.append(f"  Examples: {endpoint_summary}")
            sections.append("")

        # Per-repository details
        sections.extend(
            [
                "---",
                "",
                "## Repository Details",
                "",
            ]
        )

        for repo in context.repositories:
            sections.extend(
                [
                    f"### {repo.full_name}",
                    f"**Branch:** {repo.default_branch}",
                ]
            )
            if repo.description:
                sections.append(f"**Description:** {repo.description}")

            if repo.tech_stack.frameworks:
                sections.append(f"**Frameworks:** {', '.join(repo.tech_stack.frameworks)}")

            if repo.detected_patterns:
                sections.append(f"**Patterns:** {', '.join(repo.detected_patterns)}")

            sections.append(f"**Files Analyzed:** {len(repo.key_files)}")
            sections.append("")

        # Key file contents (most important context)
        sections.extend(
            [
                "---",
                "",
                "## Key Source Files",
                "",
                "Below are the contents of key files from the codebase. "
                "Use these to understand the project structure, patterns, and implementation details.",
                "",
            ]
        )

        for file in context.all_key_files:
            # Include file header and content
            sections.extend(
                [
                    f"### `{file.path}` (Tier {file.tier}, ~{file.token_estimate} tokens)",
                    "",
                    "```",
                    file.content,
                    "```",
                    "",
                ]
            )

        # Existing documentation
        sections.extend(
            [
                "---",
                "",
                "## Existing Documentation",
                "",
            ]
        )

        if existing_docs:
            sections.append(
                "The following documentation already exists. "
                "Do NOT duplicate these topics — instead, identify gaps."
            )
            sections.append("")
            for doc in existing_docs:
                doc_info = f"- **{doc.title}** ({doc.type or 'unknown type'})"
                if doc.folder:
                    folder_path = doc.folder.get("path", "") if isinstance(doc.folder, dict) else ""
                    doc_info += f" in `{folder_path}/`"
                sections.append(doc_info)
            sections.append("")
        else:
            sections.append("*No existing documentation found.*")
            sections.append("")

        # Section taxonomy
        sections.extend(
            [
                "---",
                "",
                get_subsection_prompt(),
                "",
            ]
        )

        # Planning instructions
        sections.extend(
            [
                "---",
                "",
                "## Your Task",
                "",
                "Analyze this codebase thoroughly and create a documentation plan.",
                "",
                "Consider what documentation would be most valuable for:",
                "- **Business stakeholders** — understanding what the software does",
                "- **New developers** — getting started and understanding the codebase",
                "- **Existing developers** — reference material and architectural guidance",
                "",
                "**Important:** Include a mix of technical AND conceptual documents.",
                "Conceptual docs help non-technical stakeholders understand the product.",
                "",
                "For each planned document, specify:",
                "- **title**: Clear, descriptive title",
                "- **doc_type**: Category (overview, architecture, guide, reference, concept)",
                "- **section**: Top-level section ('technical' or 'conceptual')",
                "- **subsection**: Specific subsection (see section taxonomy above)",
                "- **purpose**: Why this doc is valuable and who it serves",
                "- **key_topics**: What should be covered (list of topics)",
                "- **source_files**: Which source files to reference when generating",
                "- **priority**: 1-5 (1 = most important, generate first)",
                "- **folder**: Target folder (usually 'blueprints')",
                "",
                "Quality over quantity. Only recommend documentation that would be genuinely useful.",
                "It's better to have 3 excellent docs than 10 mediocre ones.",
                "",
                "Use the save_documentation_plan tool to output your plan.",
            ]
        )

        return "\n".join(sections)

    def _build_tool_schema(self) -> dict[str, Any]:
        """Build the tool schema for documentation planning."""
        return {
            "name": "save_documentation_plan",
            "description": "Save the documentation plan with list of documents to generate",
            "input_schema": {
                "type": "object",
                "required": [
                    "summary",
                    "codebase_summary",
                    "planned_documents",
                    "skipped_existing",
                ],
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": (
                            "High-level assessment of documentation needs. "
                            "What's the overall state of documentation? What's most important?"
                        ),
                    },
                    "codebase_summary": {
                        "type": "string",
                        "description": (
                            "Brief summary of the tech stack and architecture. "
                            "1-2 sentences describing what this software is and how it's built."
                        ),
                    },
                    "planned_documents": {
                        "type": "array",
                        "description": "List of documents to generate, ordered by priority",
                        "items": {
                            "type": "object",
                            "required": [
                                "title",
                                "doc_type",
                                "section",
                                "subsection",
                                "purpose",
                                "key_topics",
                                "source_files",
                                "priority",
                                "folder",
                            ],
                            "properties": {
                                "title": {
                                    "type": "string",
                                    "description": "Clear, descriptive document title",
                                },
                                "doc_type": {
                                    "type": "string",
                                    "enum": DOC_TYPES,
                                    "description": "Document category",
                                },
                                "section": {
                                    "type": "string",
                                    "enum": list(VALID_SECTIONS),
                                    "description": "Top-level section: 'technical' or 'conceptual'",
                                },
                                "subsection": {
                                    "type": "string",
                                    "enum": list(VALID_SUBSECTIONS),
                                    "description": (
                                        "Subsection within the section. "
                                        "Technical: infrastructure, frontend, backend, database, "
                                        "integrations, code-quality, security, performance. "
                                        "Conceptual: overview, concepts, workflows, glossary."
                                    ),
                                },
                                "purpose": {
                                    "type": "string",
                                    "description": "Why this doc is valuable and who it serves",
                                },
                                "key_topics": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "Topics to cover in this document",
                                },
                                "source_files": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "File paths to reference when generating",
                                },
                                "priority": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "maximum": 5,
                                    "description": "Priority 1-5 (1 = most important)",
                                },
                                "folder": {
                                    "type": "string",
                                    "description": "Target folder (e.g., 'blueprints', 'plans')",
                                },
                            },
                        },
                    },
                    "skipped_existing": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Titles of existing docs that already cover certain areas. "
                            "Explain what topics these docs already handle."
                        ),
                    },
                },
            },
        }

    def _parse_response(self, response: anthropic.types.Message) -> DocumentationPlan:
        """Parse Claude's response to extract the documentation plan."""
        for block in response.content:
            if block.type == "tool_use" and block.name == "save_documentation_plan":
                data = cast(dict[str, Any], block.input)

                # Parse planned documents
                planned_docs: list[PlannedDocument] = []
                raw_docs = data.get("planned_documents", [])

                for raw_doc in raw_docs:
                    if isinstance(raw_doc, dict):
                        planned_docs.append(
                            PlannedDocument(
                                title=raw_doc.get("title", "Untitled"),
                                doc_type=raw_doc.get("doc_type", "overview"),
                                purpose=raw_doc.get("purpose", ""),
                                key_topics=raw_doc.get("key_topics", []),
                                source_files=raw_doc.get("source_files", []),
                                priority=raw_doc.get("priority", 3),
                                folder=raw_doc.get("folder", "blueprints"),
                                section=raw_doc.get("section", "technical"),
                                subsection=raw_doc.get("subsection", "overview"),
                            )
                        )

                # Sort by priority
                planned_docs.sort(key=lambda d: d.priority)

                return DocumentationPlan(
                    summary=data.get("summary", ""),
                    planned_documents=planned_docs,
                    skipped_existing=data.get("skipped_existing", []),
                    codebase_summary=data.get("codebase_summary", ""),
                )

        # Fallback if no valid response
        logger.warning("Claude did not return a save_documentation_plan tool use")
        return DocumentationPlan(
            summary="Planning failed - no valid response from Claude",
            planned_documents=[],
            skipped_existing=[],
            codebase_summary="",
        )
