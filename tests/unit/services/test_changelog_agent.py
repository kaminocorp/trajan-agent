"""
Tests for ChangelogAgent service.

Tests cover:
- run() finding or creating changelogs
- Changelog template follows Keep a Changelog format
- add_entry() with versioned and unreleased entries
- _insert_entry() string manipulation
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.docs.types import ChangeEntry


def _make_agent():
    """Create a ChangelogAgent bypassing __init__."""
    from app.services.docs.changelog_agent import ChangelogAgent

    agent = ChangelogAgent.__new__(ChangelogAgent)
    agent.db = AsyncMock()
    agent.product = MagicMock()
    agent.product.id = uuid.uuid4()
    agent.product.name = "Test Product"
    agent.product.user_id = uuid.uuid4()
    agent.created_by_user_id = agent.product.user_id
    agent.user_id = agent.product.user_id
    agent.github_service = None
    return agent


class TestChangelogAgentRun:
    """Tests for ChangelogAgent.run()."""

    @pytest.mark.asyncio
    @patch("app.services.docs.changelog_agent.document_ops")
    async def test_run_returns_existing_when_found(self, mock_doc_ops):
        """run() returns 'found_existing' when changelog exists."""
        existing = MagicMock()
        mock_doc_ops.get_changelog = AsyncMock(return_value=existing)

        agent = _make_agent()
        result = await agent.run()

        assert result.action == "found_existing"
        assert result.document is existing

    @pytest.mark.asyncio
    @patch("app.services.docs.changelog_agent.document_ops")
    async def test_run_creates_changelog_when_missing(self, mock_doc_ops):
        """run() creates new changelog when none exists."""
        mock_doc_ops.get_changelog = AsyncMock(return_value=None)

        agent = _make_agent()
        result = await agent.run()

        assert result.action == "created"
        # Verify db.add was called with a Document
        agent.db.add.assert_called_once()
        doc = agent.db.add.call_args[0][0]
        assert doc.type == "changelog"
        assert doc.title == "Changelog"

    @pytest.mark.asyncio
    @patch("app.services.docs.changelog_agent.document_ops")
    async def test_created_changelog_follows_keep_a_changelog(self, mock_doc_ops):
        """Created changelog follows Keep a Changelog format."""
        mock_doc_ops.get_changelog = AsyncMock(return_value=None)

        agent = _make_agent()
        await agent.run()

        doc = agent.db.add.call_args[0][0]
        assert "Keep a Changelog" in doc.content
        assert "## [Unreleased]" in doc.content
        assert "### Added" in doc.content
        assert "### Changed" in doc.content
        assert "### Fixed" in doc.content

    @pytest.mark.asyncio
    @patch("app.services.docs.changelog_agent.document_ops")
    async def test_created_changelog_includes_product_name(self, mock_doc_ops):
        """Changelog template mentions the product name."""
        mock_doc_ops.get_changelog = AsyncMock(return_value=None)

        agent = _make_agent()
        agent.product.name = "My App"
        await agent.run()

        doc = agent.db.add.call_args[0][0]
        assert "My App" in doc.content


class TestChangelogAddEntry:
    """Tests for ChangelogAgent.add_entry()."""

    @pytest.mark.asyncio
    @patch("app.services.docs.changelog_agent.document_ops")
    async def test_add_versioned_entry(self, mock_doc_ops):
        """add_entry with version creates a new version section."""
        existing = MagicMock()
        existing.content = "# Changelog\n\n---\n\n## [Unreleased]\n\n---\n"
        mock_doc_ops.get_changelog = AsyncMock(return_value=existing)

        agent = _make_agent()
        changes = [ChangeEntry(category="Added", description="New feature")]

        await agent.add_entry("1.0.0", changes)

        assert "## [1.0.0]" in existing.content

    @pytest.mark.asyncio
    @patch("app.services.docs.changelog_agent.document_ops")
    async def test_add_unreleased_entry(self, mock_doc_ops):
        """add_entry with version=None adds to [Unreleased] section."""
        existing = MagicMock()
        existing.content = "# Changelog\n\n## [Unreleased]\n\n---\n"
        mock_doc_ops.get_changelog = AsyncMock(return_value=existing)

        agent = _make_agent()
        changes = [ChangeEntry(category="Fixed", description="Bug fix")]

        await agent.add_entry(None, changes)

        assert "Bug fix" in existing.content

    @pytest.mark.asyncio
    @patch("app.services.docs.changelog_agent.document_ops")
    async def test_add_entry_creates_changelog_if_missing(self, mock_doc_ops):
        """add_entry creates changelog first if none exists."""
        mock_doc_ops.get_changelog = AsyncMock(return_value=None)

        agent = _make_agent()
        changes = [ChangeEntry(category="Added", description="Initial release")]

        await agent.add_entry("0.1.0", changes)

        # A changelog document was created first (db.add called)
        agent.db.add.assert_called()


class TestChangelogInsertEntry:
    """Tests for _insert_entry string manipulation."""

    def test_insert_versioned_entry_after_separator(self):
        """Versioned entry is inserted after --- separator."""
        agent = _make_agent()
        content = "# Changelog\n\n## [Unreleased]\n\n---\n"
        changes = [ChangeEntry(category="Added", description="Feature X")]

        result = agent._insert_entry(content, "1.0.0", changes)

        assert "## [1.0.0]" in result
        assert "Feature X" in result

    def test_insert_empty_changes_returns_original(self):
        """Empty changes list returns content unchanged."""
        agent = _make_agent()
        content = "# Changelog\n\nOriginal content"

        result = agent._insert_entry(content, "1.0.0", [])

        assert result == content

    def test_insert_groups_by_category(self):
        """Entries are grouped by category."""
        agent = _make_agent()
        content = "# Changelog\n\n---\n\n## [Unreleased]\n\n---\n"
        changes = [
            ChangeEntry(category="Added", description="Feature A"),
            ChangeEntry(category="Fixed", description="Bug B"),
            ChangeEntry(category="Added", description="Feature C"),
        ]

        result = agent._insert_entry(content, "2.0.0", changes)

        assert "### Added" in result
        assert "### Fixed" in result
        assert "Feature A" in result
        assert "Bug B" in result
        assert "Feature C" in result
