"""
Tests for documentation agents.

Tests cover:
- PlansAgent folder classification heuristics
- Document folder movement operations
"""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.domain.document_operations import DocumentOperations
from app.models.document import Document
from app.models.product import Product
from app.services.docs.plans_agent import PlansAgent


class TestPlansAgentFolderClassification:
    """Tests for PlansAgent folder classification heuristics."""

    def setup_method(self) -> None:
        """Set up test fixtures."""
        self.db = MagicMock()
        self.product = Product(
            id=MagicMock(),
            user_id=MagicMock(),
            name="Test Product",
        )
        self.github_service = MagicMock()
        self.agent = PlansAgent(self.db, self.product, self.github_service, user_id=MagicMock())

    def _create_plan(self, title: str, content: str, folder: str | None = None) -> Document:
        """Helper to create a plan document."""
        doc = Document(
            id=MagicMock(),
            product_id=self.product.id,
            user_id=self.product.user_id,
            title=title,
            content=content,
            type="plan",
            folder={"path": folder} if folder else None,
        )
        return doc

    def test_completion_indicators_detected(self) -> None:
        """Plans with completion words should go to completions."""
        plan = self._create_plan(
            "Feature X",
            "This feature has been completed and shipped to production.",
            "plans",
        )
        result = self.agent._determine_correct_folder(plan)
        assert result == "completions"

    def test_done_indicator_detected(self) -> None:
        """Plans marked as done should go to completions."""
        plan = self._create_plan(
            "Refactor Y",
            "The refactoring is done. All tests passing.",
            "plans",
        )
        result = self.agent._determine_correct_folder(plan)
        assert result == "completions"

    def test_implemented_indicator_detected(self) -> None:
        """Plans marked as implemented should go to completions."""
        plan = self._create_plan(
            "API Endpoint",
            "This endpoint has been implemented.",
            "plans",
        )
        result = self.agent._determine_correct_folder(plan)
        assert result == "completions"

    def test_in_progress_indicators_detected(self) -> None:
        """Plans in progress should go to executing."""
        plan = self._create_plan(
            "New Feature",
            "Currently working on the authentication module.",
            "plans",
        )
        result = self.agent._determine_correct_folder(plan)
        assert result == "executing"

    def test_building_indicator_detected(self) -> None:
        """Plans being built should go to executing."""
        plan = self._create_plan(
            "Database Layer",
            "We are building the new ORM layer.",
            "plans",
        )
        result = self.agent._determine_correct_folder(plan)
        assert result == "executing"

    def test_archive_indicators_detected(self) -> None:
        """Plans marked as archived should go to archive."""
        plan = self._create_plan(
            "Old Feature",
            "This feature has been deprecated and superseded by Feature X.",
            "plans",
        )
        result = self.agent._determine_correct_folder(plan)
        assert result == "archive"

    def test_cancelled_indicator_detected(self) -> None:
        """Cancelled plans should go to archive."""
        plan = self._create_plan(
            "Cancelled Feature",
            "This plan was cancelled due to changing requirements.",
            "plans",
        )
        result = self.agent._determine_correct_folder(plan)
        assert result == "archive"

    def test_title_completion_indicator(self) -> None:
        """Title with completion word should go to completions."""
        plan = self._create_plan(
            "Completion Report: Auth Module",
            "Summary of the authentication implementation.",
            "plans",
        )
        result = self.agent._determine_correct_folder(plan)
        assert result == "completions"

    def test_title_wip_indicator(self) -> None:
        """Title with WIP should go to executing."""
        plan = self._create_plan(
            "WIP: New Dashboard",
            "Planning the new dashboard layout.",
            "plans",
        )
        result = self.agent._determine_correct_folder(plan)
        assert result == "executing"

    def test_default_folder_is_plans(self) -> None:
        """Plans without indicators should stay in plans."""
        plan = self._create_plan(
            "Future Feature",
            "We should add this feature in the next quarter.",
            "blueprints",  # Wrong folder
        )
        result = self.agent._determine_correct_folder(plan)
        assert result == "plans"

    def test_case_insensitive_detection(self) -> None:
        """Detection should be case insensitive."""
        plan = self._create_plan(
            "Feature",
            "This has been COMPLETED successfully.",
            "plans",
        )
        result = self.agent._determine_correct_folder(plan)
        assert result == "completions"


class TestDocumentOperationsMove:
    """Tests for document folder movement operations."""

    def setup_method(self) -> None:
        """Set up test fixtures."""
        self.ops = DocumentOperations()

    @pytest.mark.asyncio
    async def test_move_to_folder_updates_folder(self) -> None:
        """move_to_folder should update the document's folder."""
        mock_db = AsyncMock()
        mock_doc = Document(
            id=MagicMock(),
            user_id=MagicMock(),
            title="Test Doc",
            folder={"path": "plans"},
            sync_status=None,
        )

        with patch.object(self.ops, "get", return_value=mock_doc):
            result = await self.ops.move_to_folder(mock_db, mock_doc.id, "executing")

        assert result.folder == {"path": "executing"}
        mock_db.commit.assert_called_once()
        mock_db.refresh.assert_called_once_with(mock_doc)

    @pytest.mark.asyncio
    async def test_move_to_folder_updates_sync_status(self) -> None:
        """Moving a synced doc should mark it as having local changes."""
        mock_db = AsyncMock()
        mock_doc = Document(
            id=MagicMock(),
            user_id=MagicMock(),
            title="Test Doc",
            folder={"path": "plans"},
            sync_status="synced",
        )

        with patch.object(self.ops, "get", return_value=mock_doc):
            result = await self.ops.move_to_folder(mock_db, mock_doc.id, "executing")

        assert result.sync_status == "local_changes"

    @pytest.mark.asyncio
    async def test_move_to_folder_not_found(self) -> None:
        """move_to_folder should return None if document not found."""
        mock_db = AsyncMock()

        with patch.object(self.ops, "get", return_value=None):
            result = await self.ops.move_to_folder(mock_db, MagicMock(), "executing")

        assert result is None
        mock_db.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_move_to_executing(self) -> None:
        """move_to_executing should set folder to 'executing'."""
        mock_db = AsyncMock()
        mock_doc = Document(
            id=MagicMock(),
            user_id=MagicMock(),
            title="Test Doc",
            folder={"path": "plans"},
        )

        with patch.object(self.ops, "move_to_folder", return_value=mock_doc) as mock_move:
            await self.ops.move_to_executing(mock_db, mock_doc.id)
            mock_move.assert_called_once_with(mock_db, mock_doc.id, "executing")

    @pytest.mark.asyncio
    async def test_move_to_completed_includes_date(self) -> None:
        """move_to_completed should include current date in folder path."""
        mock_db = AsyncMock()
        mock_doc = Document(
            id=MagicMock(),
            user_id=MagicMock(),
            title="Test Doc",
            folder={"path": "executing"},
        )

        with patch.object(self.ops, "move_to_folder", return_value=mock_doc) as mock_move:
            await self.ops.move_to_completed(mock_db, mock_doc.id)

            # Check that the folder path includes a date
            call_args = mock_move.call_args
            folder_path = call_args[0][2]  # Third positional arg is new_folder
            assert folder_path.startswith("completions/")
            # Check date format (YYYY-MM-DD)
            date_part = folder_path.replace("completions/", "")
            datetime.strptime(date_part, "%Y-%m-%d")  # Will raise if invalid

    @pytest.mark.asyncio
    async def test_archive(self) -> None:
        """archive should set folder to 'archive'."""
        mock_db = AsyncMock()
        mock_doc = Document(
            id=MagicMock(),
            user_id=MagicMock(),
            title="Test Doc",
            folder={"path": "plans"},
        )

        with patch.object(self.ops, "move_to_folder", return_value=mock_doc) as mock_move:
            await self.ops.archive(mock_db, mock_doc.id)
            mock_move.assert_called_once_with(mock_db, mock_doc.id, "archive")
