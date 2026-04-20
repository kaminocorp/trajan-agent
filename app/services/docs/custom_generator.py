"""
CustomDocGenerator - Generates custom documentation based on user requests.

This is a standalone generator for custom documentation requests, separate from
the batch documentation orchestrator. It handles single-document, user-initiated
generation with a different progress UX pattern (modal-based vs page-level status).

Key features:
1. Single document generation (not batch)
2. User-specified parameters (doc type, format, audience)
3. Optional file focus for targeted documentation
4. Immediate content return (preview mode) or save to database
5. Progress reporting for background jobs via job store
"""

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, cast
from uuid import UUID

import anthropic
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.rls import set_rls_user_context
from app.models.document import Document
from app.models.product import Product
from app.models.repository import Repository
from app.services.docs.assessment_prompts import (
    ASSESSMENT_SUBSECTIONS,
    ASSESSMENT_TITLES,
    build_assessment_prompt,
)
from app.services.docs.claude_helpers import (
    MODEL_OPUS,
    MODEL_SONNET,
    call_with_retry,
)
from app.services.docs.codebase_analyzer import CodebaseAnalyzer
from app.services.docs.content_validator import ContentValidator
from app.services.docs.custom_prompts import build_custom_prompt
from app.services.docs.job_store import (
    STAGE_ANALYZING,
    STAGE_FINALIZING,
    STAGE_GENERATING,
    STAGE_PLANNING,
)
from app.services.docs.types import (
    CodebaseContext,
    CustomDocRequest,
    CustomDocResult,
    ValidationResult,
    ValidationWarning,
)
from app.services.github import GitHubService

logger = logging.getLogger(__name__)

# Document types that benefit from Opus's deeper reasoning (custom-specific)
COMPLEX_DOC_TYPES = {"technical", "wiki"}

# Generation limits
MAX_TOKENS_GENERATION = 8000

# Validation feedback loop configuration
MAX_CORRECTION_ITERATIONS = 2  # Max times to ask Claude to fix hallucinations
MIN_CONFIDENCE_THRESHOLD = 0.7  # Below this, trigger correction loop
HIGH_SEVERITY_THRESHOLD = 1  # Number of high-severity warnings that trigger correction


class CustomDocGenerator:
    """
    Generates custom documentation based on user requests.

    Unlike the batch DocumentGenerator, this handles single-document requests
    with user-specified parameters for doc type, format style, and audience.
    """

    def __init__(
        self,
        db: AsyncSession,
        github_service: GitHubService,
        user_id: UUID,
    ) -> None:
        self.db = db
        self.github_service = github_service
        # Acting user — used to re-arm RLS context after each commit on
        # ``self.db`` so post-commit refresh SELECTs (and any further
        # generation work) still evaluate under the correct user.
        self.user_id = user_id
        self.client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)

    async def generate(
        self,
        request: CustomDocRequest,
        product: Product,
        repositories: list[Repository],
        user_id: str | UUID,
        save_immediately: bool = False,
        progress_callback: Callable[[str], Awaitable[None]] | None = None,
        cancellation_check: Callable[[], Awaitable[bool]] | None = None,
    ) -> CustomDocResult:
        """
        Generate custom documentation based on user request.

        Args:
            request: The user's custom doc request with all parameters
            product: The product this documentation belongs to
            repositories: Repositories to analyze for context
            user_id: User who owns this document
            save_immediately: If True, save as Document; if False, return content only
            progress_callback: Optional async callback for progress updates (background jobs)
            cancellation_check: Optional async callback to check if job was cancelled

        Returns:
            CustomDocResult with generated content and optionally saved Document
        """
        start_time = time.time()

        async def report_progress(stage: str) -> None:
            """Report progress if callback provided."""
            if progress_callback:
                await progress_callback(stage)

        async def check_cancelled() -> bool:
            """Check if job was cancelled."""
            if cancellation_check:
                return await cancellation_check()
            return False

        try:
            # Step 1: Analyze codebase for context
            if await check_cancelled():
                return CustomDocResult(
                    success=False,
                    error="Cancelled by user",
                    generation_time_seconds=time.time() - start_time,
                )
            await report_progress(STAGE_ANALYZING)
            logger.info(f"Analyzing codebase for custom doc: {request.prompt[:50]}...")
            context = await self._get_codebase_context(repositories, request.focus_paths)

            # Step 2: Plan document structure
            if await check_cancelled():
                return CustomDocResult(
                    success=False,
                    error="Cancelled by user",
                    generation_time_seconds=time.time() - start_time,
                )
            await report_progress(STAGE_PLANNING)

            # Step 3: Generate content using Claude with validation feedback loop
            if await check_cancelled():
                return CustomDocResult(
                    success=False,
                    error="Cancelled by user",
                    generation_time_seconds=time.time() - start_time,
                )
            await report_progress(STAGE_GENERATING)

            # Generate and validate with feedback loop
            content, suggested_title, validation_result = await self._generate_with_validation(
                request=request,
                context=context,
                check_cancelled=check_cancelled,
            )

            # Check if cancelled during generation
            if content is None:
                return CustomDocResult(
                    success=False,
                    error="Cancelled by user",
                    generation_time_seconds=time.time() - start_time,
                )

            # Step 4: Finalize
            if await check_cancelled():
                return CustomDocResult(
                    success=False,
                    error="Cancelled by user",
                    generation_time_seconds=time.time() - start_time,
                )
            await report_progress(STAGE_FINALIZING)

            # Use user's title if provided, otherwise use AI-suggested title
            final_title = request.title or suggested_title or "Untitled Document"

            # Step 3: Optionally save as Document
            document = None
            if save_immediately:
                document = await self._save_document(
                    product=product,
                    user_id=user_id,
                    title=final_title,
                    content=content,
                    doc_type=request.doc_type,
                )

            generation_time = time.time() - start_time
            logger.info(
                f"Custom document generated in {generation_time:.2f}s "
                f"(validation confidence: {validation_result.confidence_score:.0%})"
            )

            return CustomDocResult(
                success=True,
                content=content,
                suggested_title=suggested_title,
                document=document,
                generation_time_seconds=generation_time,
                # Note: validation is internal - user gets clean, validated content
            )

        except Exception as e:
            logger.error(f"Failed to generate custom document: {e}")
            return CustomDocResult(
                success=False,
                error=str(e),
                generation_time_seconds=time.time() - start_time,
            )

    async def generate_assessment(
        self,
        assessment_type: str,
        product: Product,
        repositories: list[Repository],
        user_id: str | UUID,
        progress_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> CustomDocResult:
        """
        Generate a critical assessment of the codebase.

        Assessments are always saved immediately (not previewed) and are placed
        in the appropriate technical subsection.

        Args:
            assessment_type: One of "code-quality", "security", "performance"
            product: The product this assessment belongs to
            repositories: Repositories to analyze
            user_id: User who owns this document
            progress_callback: Optional async callback for progress updates

        Returns:
            CustomDocResult with generated assessment content and saved Document
        """
        start_time = time.time()

        async def report_progress(stage: str) -> None:
            """Report progress if callback provided."""
            if progress_callback:
                await progress_callback(stage)

        try:
            # Step 1: Analyze codebase
            await report_progress(STAGE_ANALYZING)
            logger.info(f"Analyzing codebase for {assessment_type} assessment...")
            context = await self._get_codebase_context(repositories)

            # Step 2: Plan assessment
            await report_progress(STAGE_PLANNING)

            # Step 3: Generate assessment using Opus (complex reasoning)
            await report_progress(STAGE_GENERATING)
            prompt = build_assessment_prompt(assessment_type, context)

            # Always use Opus for assessments - they require deep analysis
            response = await self.client.messages.create(
                model=MODEL_OPUS,
                max_tokens=MAX_TOKENS_GENERATION,
                tools=cast(Any, [self._build_tool_schema()]),
                tool_choice=cast(Any, {"type": "tool", "name": "save_document"}),
                messages=[{"role": "user", "content": prompt}],
            )

            content, suggested_title = self._parse_response(response)

            # Step 4: Save the assessment document
            await report_progress(STAGE_FINALIZING)

            # Use predefined title for consistency
            final_title = ASSESSMENT_TITLES.get(assessment_type, suggested_title)
            subsection = ASSESSMENT_SUBSECTIONS.get(assessment_type, assessment_type)

            doc = Document(
                product_id=product.id,
                created_by_user_id=str(user_id),
                title=final_title,
                content=content,
                type="note",  # Assessments are technical notes
                is_generated=True,  # AI-generated, shown in Trajan Docs tab
                folder={"path": "blueprints"},
                section="technical",
                subsection=subsection,
            )
            self.db.add(doc)
            await self.db.commit()
            # Commit dropped SET LOCAL; re-arm before the refresh SELECT.
            await set_rls_user_context(self.db, self.user_id)
            await self.db.refresh(doc)

            generation_time = time.time() - start_time
            logger.info(f"{assessment_type} assessment generated in {generation_time:.2f}s")

            return CustomDocResult(
                success=True,
                content=content,
                suggested_title=final_title,
                document=doc,
                generation_time_seconds=generation_time,
            )

        except Exception as e:
            logger.error(f"Failed to generate {assessment_type} assessment: {e}")
            return CustomDocResult(
                success=False,
                error=str(e),
                generation_time_seconds=time.time() - start_time,
            )

    async def _get_codebase_context(
        self,
        repositories: list[Repository],
        focus_paths: list[str] | None = None,
    ) -> CodebaseContext:
        """
        Get codebase context, optionally focused on specific paths.

        If focus_paths are provided, the analyzer will prioritize those files
        in the context window.
        """
        analyzer = CodebaseAnalyzer(self.github_service)
        context = await analyzer.analyze(repositories)

        # If focus paths specified, filter/prioritize those files
        if focus_paths:
            focused_files = [
                f for f in context.all_key_files if any(fp in f.path for fp in focus_paths)
            ]
            if focused_files:
                # Put focused files first, then others
                other_files = [f for f in context.all_key_files if f not in focused_files]
                context.all_key_files = focused_files + other_files

        return context

    async def _generate_with_validation(
        self,
        request: CustomDocRequest,
        context: CodebaseContext,
        check_cancelled: Callable[[], Awaitable[bool]],
    ) -> tuple[str | None, str, ValidationResult]:
        """
        Generate content with validation feedback loop.

        If the initial generation has high-severity hallucinations, this method
        feeds the validation warnings back to Claude to correct them. The loop
        continues until validation passes or max iterations are reached.

        Args:
            request: The custom doc request
            context: Codebase analysis context
            check_cancelled: Async function to check if job was cancelled

        Returns:
            Tuple of (content, suggested_title, final_validation_result)
            Content is None if cancelled.
        """
        validator = ContentValidator(context)

        # Initial generation
        logger.info("Generating custom document content...")
        content, suggested_title = await self._call_claude(request, context)

        # Validate initial content
        validation_result = validator.validate(content)
        iteration = 0

        # Feedback loop: correct hallucinations if needed
        while self._needs_correction(validation_result) and iteration < MAX_CORRECTION_ITERATIONS:
            iteration += 1

            if await check_cancelled():
                return None, suggested_title, validation_result

            high_severity_warnings = [w for w in validation_result.warnings if w.severity == "high"]
            logger.warning(
                f"Validation found {len(high_severity_warnings)} high-severity issues "
                f"(iteration {iteration}/{MAX_CORRECTION_ITERATIONS}). Requesting correction..."
            )
            for warning in high_severity_warnings:
                logger.warning(f"  - [{warning.claim_type}] {warning.message}")

            # Build correction prompt and regenerate
            content, suggested_title = await self._call_claude_with_correction(
                request=request,
                context=context,
                previous_content=content,
                warnings=high_severity_warnings,
            )

            # Re-validate corrected content
            validation_result = validator.validate(content)

            if not self._needs_correction(validation_result):
                logger.info(
                    f"Correction successful after {iteration} iteration(s). "
                    f"Confidence: {validation_result.confidence_score:.0%}"
                )

        # Log final status
        if validation_result.has_warnings and self._needs_correction(validation_result):
            logger.warning(
                f"Validation complete with {len(validation_result.warnings)} remaining warnings "
                f"after {iteration} correction(s). Confidence: {validation_result.confidence_score:.0%}"
            )
        elif not validation_result.has_warnings:
            logger.info("Content validated successfully with no warnings.")

        return content, suggested_title, validation_result

    def _needs_correction(self, validation_result: ValidationResult) -> bool:
        """
        Determine if validation result warrants a correction attempt.

        Triggers correction if:
        - Confidence score is below threshold, OR
        - There are high-severity warnings (endpoints, models not found)
        """
        if validation_result.confidence_score < MIN_CONFIDENCE_THRESHOLD:
            return True

        high_severity_count = sum(1 for w in validation_result.warnings if w.severity == "high")
        return high_severity_count >= HIGH_SEVERITY_THRESHOLD

    async def _call_claude_with_correction(
        self,
        request: CustomDocRequest,
        context: CodebaseContext,
        previous_content: str,
        warnings: list[ValidationWarning],
    ) -> tuple[str, str]:
        """
        Call Claude with correction instructions for hallucinated content.

        Sends the previous content along with specific warnings about what
        needs to be fixed, asking Claude to regenerate without the hallucinations.
        """
        model = self._select_model(request.doc_type)
        correction_prompt = self._build_correction_prompt(
            request=request,
            context=context,
            previous_content=previous_content,
            warnings=warnings,
        )
        tool_schema = self._build_tool_schema()

        response = await self.client.messages.create(
            model=model,
            max_tokens=MAX_TOKENS_GENERATION,
            tools=cast(Any, [tool_schema]),
            tool_choice=cast(Any, {"type": "tool", "name": "save_document"}),
            messages=[{"role": "user", "content": correction_prompt}],
        )

        return self._parse_response(response)

    def _build_correction_prompt(
        self,
        request: CustomDocRequest,  # noqa: ARG002 - Reserved for future use
        context: CodebaseContext,
        previous_content: str,
        warnings: list[ValidationWarning],
    ) -> str:
        """
        Build a prompt asking Claude to correct hallucinated content.

        The prompt includes:
        1. The original request context
        2. The previous (problematic) content
        3. Specific warnings about what was hallucinated
        4. Clear instructions to remove/fix the issues
        """
        # Group warnings by type for clearer presentation
        endpoint_issues = [w for w in warnings if w.claim_type == "endpoint"]
        model_issues = [w for w in warnings if w.claim_type == "model"]
        tech_issues = [w for w in warnings if w.claim_type == "technology"]

        sections = [
            "# Documentation Correction Required",
            "",
            "You previously generated documentation that contains **hallucinated content** — ",
            "references to features, endpoints, or models that do not exist in the actual codebase.",
            "",
            "## What Needs to Be Fixed",
            "",
        ]

        if endpoint_issues:
            sections.append("### Non-Existent API Endpoints")
            sections.append(
                "The following endpoints were mentioned but DO NOT EXIST in the codebase:"
            )
            for w in endpoint_issues:
                sections.append(f"- `{w.claim}` — Remove or replace this reference")
            sections.append("")

        if model_issues:
            sections.append("### Non-Existent Models/Entities")
            sections.append("The following models were mentioned but DO NOT EXIST in the codebase:")
            for w in model_issues:
                sections.append(f"- `{w.claim}` — Remove or replace this reference")
            sections.append("")

        if tech_issues:
            sections.append("### Unverified Technologies")
            sections.append(
                "The following technologies were mentioned but not detected in the codebase:"
            )
            for w in tech_issues:
                sections.append(f"- `{w.claim}` — Verify this is actually used or remove")
            sections.append("")

        # Add the actual tech stack and models for reference
        tech = context.combined_tech_stack
        sections.extend(
            [
                "## What Actually Exists in the Codebase",
                "",
                "**Technologies detected:**",
                f"- Languages: {', '.join(tech.languages) if tech.languages else 'None detected'}",
                f"- Frameworks: {', '.join(tech.frameworks) if tech.frameworks else 'None detected'}",
                f"- Databases: {', '.join(tech.databases) if tech.databases else 'None detected'}",
                "",
            ]
        )

        if context.all_models:
            sections.append("**Models detected:**")
            for model in context.all_models[:15]:  # Limit to avoid huge prompts
                sections.append(f"- `{model.name}` ({model.model_type})")
            sections.append("")

        if context.all_endpoints:
            sections.append("**API Endpoints detected:**")
            for ep in context.all_endpoints[:20]:  # Limit to avoid huge prompts
                sections.append(f"- `{ep.method} {ep.path}`")
            sections.append("")

        sections.extend(
            [
                "## Your Previous Content (Contains Errors)",
                "",
                "```markdown",
                previous_content,
                "```",
                "",
                "## Instructions",
                "",
                "1. **Remove all hallucinated references** listed above",
                "2. **Only reference endpoints, models, and technologies that actually exist**",
                "3. **If you can't find code evidence for something, don't include it**",
                "4. **Keep the same overall structure and purpose** of the document",
                "5. **If removing content leaves gaps, either:**",
                "   - State that the feature doesn't exist in the current codebase, OR",
                "   - Replace with accurate information about what DOES exist",
                "",
                "Use the `save_document` tool to output the corrected documentation.",
            ]
        )

        return "\n".join(sections)

    async def _save_document(
        self,
        product: Product,
        user_id: str | UUID,
        title: str,
        content: str,
        doc_type: str,
    ) -> Document:
        """Save generated content as a Document entity."""
        doc = Document(
            product_id=product.id,
            created_by_user_id=str(user_id),
            title=title,
            content=content,
            type=doc_type,
            is_generated=True,
            folder={"path": "blueprints"},  # Default folder for custom docs
        )
        self.db.add(doc)
        await self.db.commit()
        # Commit dropped SET LOCAL; re-arm before the refresh SELECT.
        await set_rls_user_context(self.db, self.user_id)
        await self.db.refresh(doc)
        logger.info(f"Saved custom document: {title}")
        return doc

    def _select_model(self, doc_type: str) -> str:
        """Select model — Opus for technical/wiki, Sonnet otherwise."""
        if doc_type in COMPLEX_DOC_TYPES:
            return MODEL_OPUS
        return MODEL_SONNET

    async def _call_claude(
        self,
        request: CustomDocRequest,
        context: CodebaseContext,
    ) -> tuple[str, str]:
        """
        Call Claude API to generate document content.

        Returns:
            Tuple of (content, suggested_title)
        """
        model = self._select_model(request.doc_type)
        prompt = build_custom_prompt(request, context)
        tool_schema = self._build_tool_schema()

        async def _do_call() -> tuple[str, str]:
            response = await self.client.messages.create(
                model=model,
                max_tokens=MAX_TOKENS_GENERATION,
                tools=cast(Any, [tool_schema]),
                tool_choice=cast(Any, {"type": "tool", "name": "save_document"}),
                messages=[{"role": "user", "content": prompt}],
            )
            return self._parse_response(response)

        return await call_with_retry(_do_call, operation_name="Custom doc generation")

    def _build_tool_schema(self) -> dict[str, Any]:
        """Build the tool schema for document generation."""
        return {
            "name": "save_document",
            "description": "Save the generated documentation with a suggested title",
            "input_schema": {
                "type": "object",
                "required": ["content", "suggested_title"],
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "The full markdown content of the document.",
                    },
                    "suggested_title": {
                        "type": "string",
                        "description": (
                            "A concise, descriptive title for this document (2-6 words). "
                            "Should reflect the main topic covered."
                        ),
                    },
                },
            },
        }

    def _parse_response(self, response: anthropic.types.Message) -> tuple[str, str]:
        """
        Parse Claude's response to extract document content and title.

        Returns:
            Tuple of (content, suggested_title)
        """
        for block in response.content:
            if block.type == "tool_use" and block.name == "save_document":
                data = cast(dict[str, Any], block.input)
                content = data.get("content", "")
                title = data.get("suggested_title", "Untitled Document")
                if isinstance(content, str) and isinstance(title, str):
                    return content, title

        logger.warning("Claude did not return a save_document tool use")
        return "Content generation failed.", "Untitled Document"
