"""
DocumentGenerator - Generates individual documents from a documentation plan.

This is the execution engine of Documentation Agent v2. It takes a PlannedDocument
from the DocumentationPlanner and generates the actual content using Claude.

Key design decisions:
1. One document at a time — focused context, quality over quantity
2. Smart model selection — Opus 4.5 for complex docs, Sonnet for simpler ones
3. Relevant context only — extracts source files specified in the plan
4. Database persistence — saves each document immediately after generation
"""

import logging
from typing import Any, cast
from uuid import UUID

import anthropic
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.rls import set_rls_user_context
from app.models.document import Document
from app.models.product import Product
from app.services.docs.claude_helpers import call_with_retry, select_model
from app.services.docs.custom_prompts import AUDIENCE_INSTRUCTIONS
from app.services.docs.types import (
    BatchGeneratorResult,
    CodebaseContext,
    DocumentationPlan,
    FileContent,
    GeneratorResult,
    PlannedDocument,
)

logger = logging.getLogger(__name__)

# Generation limits
MAX_TOKENS_GENERATION = 8000
MAX_CONTEXT_TOKENS = 50000  # Per-document context budget


class DocumentGenerator:
    """
    Generates individual documents based on a documentation plan.

    Takes PlannedDocument specifications from the DocumentationPlanner and
    generates actual markdown content using Claude. Each document is generated
    with focused context from relevant source files.
    """

    def __init__(self, db: AsyncSession, user_id: UUID) -> None:
        self.db = db
        # Acting user — used to re-arm RLS context after every per-doc
        # commit so subsequent refresh/SELECTs still evaluate under the
        # correct user. The ``after_begin`` listener reads this via
        # ``session.info`` too; the explicit re-call below is the
        # belt-and-braces layer.
        self.user_id = user_id
        self.client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    async def generate(
        self,
        planned_doc: PlannedDocument,
        codebase_context: CodebaseContext,
        product: Product,
        created_by_user_id: str | UUID,
    ) -> GeneratorResult:
        """
        Generate a single document based on the plan.

        Args:
            planned_doc: Specification from DocumentationPlanner
            codebase_context: Full codebase analysis (from CodebaseAnalyzer)
            product: The product this documentation belongs to
            created_by_user_id: User who is creating this document (for audit)

        Returns:
            GeneratorResult with the created Document or error details
        """
        try:
            # Extract relevant source files for this document
            relevant_files = self._extract_relevant_files(
                planned_doc.source_files,
                codebase_context.all_key_files,
            )

            # Generate content using Claude
            content = await self._call_claude(planned_doc, relevant_files, codebase_context)

            # Create and save the document
            doc = Document(
                product_id=product.id,
                created_by_user_id=str(created_by_user_id),
                title=planned_doc.title,
                content=content,
                type=planned_doc.doc_type,
                folder={"path": planned_doc.folder},
                section=planned_doc.section,
                subsection=planned_doc.subsection,
                is_generated=True,  # AI-generated document
            )
            self.db.add(doc)
            await self.db.commit()
            # Commit dropped SET LOCAL; re-arm before the refresh SELECT
            # and any subsequent sub-agents running on the same session.
            await set_rls_user_context(self.db, self.user_id)
            await self.db.refresh(doc)

            logger.info(f"Generated document: {planned_doc.title}")
            return GeneratorResult(document=doc, success=True)

        except Exception as e:
            logger.error(f"Failed to generate document '{planned_doc.title}': {e}")
            return GeneratorResult(document=None, success=False, error=str(e))

    async def generate_batch(
        self,
        plan: DocumentationPlan,
        codebase_context: CodebaseContext,
        product: Product,
        created_by_user_id: str | UUID,
        on_progress: Any | None = None,
    ) -> BatchGeneratorResult:
        """
        Generate all documents in a plan sequentially.

        Args:
            plan: Complete documentation plan from DocumentationPlanner
            codebase_context: Full codebase analysis
            product: The product this documentation belongs to
            created_by_user_id: User who is creating these documents (for audit)
            on_progress: Optional callback(current: int, total: int, title: str)

        Returns:
            BatchGeneratorResult with all generated documents and failures
        """
        result = BatchGeneratorResult(
            total_planned=len(plan.planned_documents),
        )

        for i, planned_doc in enumerate(plan.planned_documents):
            # Report progress
            if on_progress:
                try:
                    await on_progress(i + 1, len(plan.planned_documents), planned_doc.title)
                except Exception as e:
                    logger.warning(f"Progress callback failed: {e}")

            # Generate the document
            gen_result = await self.generate(
                planned_doc=planned_doc,
                codebase_context=codebase_context,
                product=product,
                created_by_user_id=created_by_user_id,
            )

            if gen_result.success and gen_result.document:
                result.documents.append(gen_result.document)
                result.total_generated += 1
            else:
                result.failed.append(planned_doc.title)
                logger.warning(f"Failed to generate '{planned_doc.title}': {gen_result.error}")

        return result

    def _extract_relevant_files(
        self,
        requested_paths: list[str],
        all_files: list[FileContent],
    ) -> list[FileContent]:
        """
        Extract files relevant to this document from the full codebase context.

        Matches files by:
        1. Exact path match
        2. Path contains the requested pattern (for directories)
        3. Filename match (for flexible references)

        Respects token budget to avoid overwhelming the model.
        """
        relevant: list[FileContent] = []
        total_tokens = 0

        # Build lookup for faster matching
        file_by_path = {f.path: f for f in all_files}

        for requested in requested_paths:
            # Try exact match first
            if requested in file_by_path:
                file = file_by_path[requested]
                if total_tokens + file.token_estimate <= MAX_CONTEXT_TOKENS:
                    relevant.append(file)
                    total_tokens += file.token_estimate
                continue

            # Try pattern matching (for directory references like "backend/app/api/")
            for file in all_files:
                if file in relevant:
                    continue

                # Check if path contains the pattern or filename matches
                matches = requested in file.path or file.path.endswith(requested)
                within_budget = total_tokens + file.token_estimate <= MAX_CONTEXT_TOKENS
                if matches and within_budget:
                    relevant.append(file)
                    total_tokens += file.token_estimate

        # If no specific files matched, include tier 1 files as baseline
        if not relevant:
            for file in all_files:
                if file.tier == 1 and total_tokens + file.token_estimate <= MAX_CONTEXT_TOKENS:
                    relevant.append(file)
                    total_tokens += file.token_estimate

        return relevant

    async def _call_claude(
        self,
        planned_doc: PlannedDocument,
        relevant_files: list[FileContent],
        context: CodebaseContext,
    ) -> str:
        """Call Claude API to generate document content."""
        model = select_model(planned_doc.doc_type)
        prompt = self._build_prompt(planned_doc, relevant_files, context)
        tool_schema = self._build_tool_schema(planned_doc)

        async def _do_call() -> str:
            response = await self.client.messages.create(
                model=model,
                max_tokens=MAX_TOKENS_GENERATION,
                tools=cast(Any, [tool_schema]),
                tool_choice=cast(Any, {"type": "tool", "name": "save_document"}),
                messages=[{"role": "user", "content": prompt}],
            )
            return self._parse_response(response, planned_doc)

        return await call_with_retry(_do_call, operation_name="Document generation")

    def _build_prompt(
        self,
        planned_doc: PlannedDocument,
        relevant_files: list[FileContent],
        context: CodebaseContext,
    ) -> str:
        """Build the prompt for document generation."""
        # Determine audience based on section
        is_conceptual = planned_doc.section == "conceptual"
        audience_key = "internal-non-technical" if is_conceptual else "internal-technical"
        audience_instruction = AUDIENCE_INSTRUCTIONS.get(
            audience_key, "Write for a general technical audience."
        )

        sections = [
            "You are writing documentation for a software project.",
            "",
            "---",
            "",
            "## Document Specification",
            "",
            f"**Title:** {planned_doc.title}",
            f"**Type:** {planned_doc.doc_type}",
            f"**Section:** {planned_doc.section} / {planned_doc.subsection}",
            f"**Purpose:** {planned_doc.purpose}",
            "",
            "## Target Audience",
            "",
            audience_instruction,
            "",
        ]

        # Key topics to cover
        if planned_doc.key_topics:
            sections.append("**Key Topics to Cover:**")
            for topic in planned_doc.key_topics:
                sections.append(f"- {topic}")
            sections.append("")

        # Tech stack context (brief)
        sections.extend(
            [
                "---",
                "",
                "## Project Context",
                "",
            ]
        )

        tech = context.combined_tech_stack
        if tech.languages:
            sections.append(f"**Languages:** {', '.join(tech.languages)}")
        if tech.frameworks:
            sections.append(f"**Frameworks:** {', '.join(tech.frameworks)}")
        if tech.databases:
            sections.append(f"**Databases:** {', '.join(tech.databases)}")
        if context.detected_patterns:
            sections.append(f"**Architecture:** {', '.join(context.detected_patterns)}")
        sections.append("")

        # Relevant source files
        if relevant_files:
            sections.extend(
                [
                    "---",
                    "",
                    "## Source Files",
                    "",
                    "Use these source files as reference for accurate, specific documentation:",
                    "",
                ]
            )

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

        # Document type-specific instructions
        sections.extend(self._get_type_instructions(planned_doc))

        return "\n".join(sections)

    def _get_type_instructions(self, planned_doc: PlannedDocument) -> list[str]:
        """Get writing instructions specific to the document type."""
        base_instructions = [
            "---",
            "",
            "## Writing Instructions",
            "",
        ]

        type_specific: dict[str, list[str]] = {
            "overview": [
                "Write a comprehensive project overview that:",
                "- Explains what this software does and why it exists",
                "- Describes the key features and capabilities",
                "- Provides a high-level view of how it works",
                "- Helps new team members understand the project quickly",
                "",
                "Tone: Welcoming, clear, accessible to both technical and non-technical readers.",
            ],
            "architecture": [
                "Write a technical architecture document that:",
                "- Describes the system design and major components",
                "- Explains how data flows through the system",
                "- Documents key technical decisions and trade-offs",
                "- Includes diagrams descriptions where helpful (use ASCII or mermaid)",
                "",
                "Tone: Technical, precise, focused on implementation details.",
            ],
            "guide": [
                "Write a practical how-to guide that:",
                "- Provides step-by-step instructions",
                "- Includes working code examples",
                "- Covers common use cases and edge cases",
                "- Anticipates questions and troubleshooting",
                "",
                "Tone: Instructional, practical, action-oriented.",
            ],
            "reference": [
                "Write a reference document that:",
                "- Provides complete, accurate technical details",
                "- Uses consistent formatting for easy scanning",
                "- Includes all parameters, options, and return values",
                "- Is organized for quick lookup",
                "",
                "Tone: Factual, comprehensive, well-structured.",
            ],
            "concept": [
                "Write a conceptual document that:",
                "- Explains the mental model behind this concept",
                "- Uses analogies and examples to build understanding",
                "- Connects this concept to related concepts",
                "- Helps readers develop intuition, not just knowledge",
                "",
                "Tone: Educational, thoughtful, builds deep understanding.",
            ],
        }

        instructions = type_specific.get(
            planned_doc.doc_type,
            [
                "Write clear, well-structured documentation.",
                "Be specific and accurate based on the source files.",
            ],
        )

        return (
            base_instructions
            + instructions
            + [
                "",
                "**Format:** Use markdown with clear headings, bullet points, and code blocks.",
                "**Length:** Be thorough but concise. Quality over quantity.",
                "**Accuracy:** Only document what you can verify from the source files.",
                "",
                "Use the save_document tool to output your documentation.",
            ]
        )

    def _build_tool_schema(self, planned_doc: PlannedDocument) -> dict[str, Any]:
        """Build the tool schema for document generation."""
        return {
            "name": "save_document",
            "description": f"Save the generated {planned_doc.doc_type} document",
            "input_schema": {
                "type": "object",
                "required": ["content"],
                "properties": {
                    "content": {
                        "type": "string",
                        "description": (
                            f"The full markdown content of the {planned_doc.doc_type} document. "
                            f"Should have '{planned_doc.title}' as the main heading."
                        ),
                    },
                },
            },
        }

    def _parse_response(
        self,
        response: anthropic.types.Message,
        planned_doc: PlannedDocument,
    ) -> str:
        """Parse Claude's response to extract document content."""
        for block in response.content:
            if block.type == "tool_use" and block.name == "save_document":
                data = cast(dict[str, Any], block.input)
                content = data.get("content")
                if isinstance(content, str):
                    return content
                return f"# {planned_doc.title}\n\nContent generation failed."

        logger.warning("Claude did not return a save_document tool use")
        return f"# {planned_doc.title}\n\nContent generation failed."
