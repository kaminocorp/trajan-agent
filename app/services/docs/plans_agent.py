"""
PlansAgent - Manages plan lifecycle through folder organization.

Responsible for:
- plans/ - Active plans and roadmaps
- executing/ - Plans currently being implemented
- completions/ - Completed work with date prefixes
- archive/ - Archived or deprecated plans

Tier 1 (Scan & Structure):
- Maps imported plan docs to correct folder
- Ensures folder structure exists

Tier 2 (Maintenance):
- Creates new plans
- Moves plans through lifecycle (plans → executing → completions → archive)
"""

import logging
import uuid as uuid_pkg
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rls import set_rls_user_context
from app.domain.document_operations import document_ops
from app.models.document import Document
from app.models.product import Product
from app.services.docs.types import PlansResult
from app.services.github import GitHubService

logger = logging.getLogger(__name__)


class PlansAgent:
    """
    Agent responsible for plans/, executing/, completions/, archive/ folders.

    Manages the lifecycle of plan documents as they move from ideation
    through execution to completion.
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
        # Acting user — used to re-arm RLS context after commits.
        self.user_id = user_id

    async def run(self) -> PlansResult:
        """
        Ensure plans structure exists, organize any imported plans.

        Returns:
            PlansResult with count of organized plans
        """
        # Get all plan-type documents
        plans = await self._get_plan_documents()

        if not plans:
            logger.info(f"No plan documents found for product {self.product.id}")
            return PlansResult(organized_count=0)

        # Ensure they're in correct folders based on content analysis
        organized_count = 0
        for plan in plans:
            correct_folder = self._determine_correct_folder(plan)
            current_path = plan.folder.get("path") if plan.folder else None

            if current_path != correct_folder:
                plan.folder = {"path": correct_folder}
                plan.updated_at = datetime.now(UTC)
                organized_count += 1
                logger.info(
                    f"Moved plan '{plan.title}' from '{current_path}' to '{correct_folder}'"
                )

        if organized_count > 0:
            await self.db.commit()
            # Re-arm context — ``self.db`` is shared with the orchestrator
            # and may have further work queued after this agent returns.
            await set_rls_user_context(self.db, self.user_id)

        return PlansResult(organized_count=organized_count)

    async def _get_plan_documents(self) -> list[Document]:
        """Get all plan-type documents for this product."""
        if self.product.id is None:
            return []

        all_docs = await document_ops.get_by_product(self.db, self.product.id)
        return [doc for doc in all_docs if doc.type == "plan"]

    def _determine_correct_folder(self, plan: Document) -> str:
        """
        Analyze plan content to determine correct folder.

        Uses simple heuristics - could be enhanced with Claude for more
        accurate classification.
        """
        content_lower = (plan.content or "").lower()
        title_lower = (plan.title or "").lower()

        # Check for completion indicators
        completion_indicators = [
            "completed",
            "done",
            "finished",
            "shipped",
            "released",
            "implemented",
        ]
        if any(word in content_lower for word in completion_indicators):
            return "completions"

        # Check for in-progress indicators
        progress_indicators = [
            "in progress",
            "currently",
            "working on",
            "implementing",
            "building",
            "developing",
        ]
        if any(word in content_lower for word in progress_indicators):
            return "executing"

        # Check for archive indicators
        archive_indicators = [
            "archived",
            "deprecated",
            "obsolete",
            "superseded",
            "abandoned",
            "cancelled",
        ]
        if any(word in content_lower for word in archive_indicators):
            return "archive"

        # Check title for hints
        if "completion" in title_lower or "report" in title_lower:
            return "completions"
        if "wip" in title_lower or "draft" in title_lower:
            return "executing"

        # Default: keep in plans folder
        return "plans"

    async def create_plan(
        self,
        title: str,
        content: str,
    ) -> Document:
        """
        Tier 2: Create a new plan document.

        Args:
            title: Plan title
            content: Plan content (markdown)

        Returns:
            Created plan document
        """
        doc = Document(
            product_id=self.product.id,
            created_by_user_id=self.product.user_id,
            title=title,
            content=content,
            type="plan",
            folder={"path": "plans"},
        )
        self.db.add(doc)
        await self.db.commit()
        # Commit dropped SET LOCAL; re-arm before the refresh SELECT.
        await set_rls_user_context(self.db, self.user_id)
        await self.db.refresh(doc)

        logger.info(f"Created plan: {title}")
        return doc

    async def move_to_executing(
        self,
        document_id: uuid_pkg.UUID,
    ) -> Document | None:
        """
        Tier 2: Move a plan to executing/ folder.

        Args:
            document_id: ID of the plan to move

        Returns:
            Updated document, or None if not found
        """
        doc = await document_ops.move_to_executing(self.db, document_id)
        if doc:
            logger.info(f"Moved plan '{doc.title}' to executing/")
        else:
            logger.warning(f"Document {document_id} not found")
        return doc

    async def move_to_completed(
        self,
        document_id: uuid_pkg.UUID,
    ) -> Document | None:
        """
        Tier 2: Move a plan to completions/ folder.

        Includes date prefix in folder path for chronological organization.

        Args:
            document_id: ID of the plan to move

        Returns:
            Updated document, or None if not found
        """
        doc = await document_ops.move_to_completed(self.db, document_id)
        if doc:
            folder_path = doc.folder.get("path") if doc.folder else "completions"
            logger.info(f"Moved plan '{doc.title}' to {folder_path}/")
        else:
            logger.warning(f"Document {document_id} not found")
        return doc

    async def archive(
        self,
        document_id: uuid_pkg.UUID,
    ) -> Document | None:
        """
        Tier 2: Move a document to archive/ folder.

        Args:
            document_id: ID of the document to archive

        Returns:
            Updated document, or None if not found
        """
        doc = await document_ops.archive(self.db, document_id)
        if doc:
            logger.info(f"Archived document '{doc.title}'")
        else:
            logger.warning(f"Document {document_id} not found")
        return doc
