"""
Tests for BlueprintAgent service.

Tests cover:
- run() creating overview and architecture docs
- Complexity detection (_is_complex_project)
- Claude tool-use response mocking pattern
- Graceful handling of generation failures
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import pytest

from app.services.docs.types import BlueprintResult, DocumentSpec


def _make_agent():
    """Create a BlueprintAgent bypassing __init__."""
    from app.services.docs.blueprint_agent import BlueprintAgent

    agent = BlueprintAgent.__new__(BlueprintAgent)
    agent.db = AsyncMock()
    agent.product = MagicMock()
    agent.product.id = uuid.uuid4()
    agent.product.name = "Test Product"
    agent.product.description = "A test project"
    agent.product.user_id = uuid.uuid4()
    agent.github_service = AsyncMock()
    agent.client = AsyncMock()
    agent.user_id = uuid.uuid4()
    return agent


def _make_repo_context(full_name="org/repo", file_count=10):
    """Create a mock RepoContext with controllable file count."""
    ctx = MagicMock()
    ctx.full_name = full_name
    ctx.default_branch = "main"
    ctx.description = "Test repo"
    ctx.languages = []
    ctx.tree = MagicMock()
    ctx.tree.files = [f"file_{i}.py" for i in range(file_count)]
    ctx.files = {}
    return ctx


def _make_mock_claude_response(content: str) -> MagicMock:
    """Create a mock anthropic.types.Message with save_document tool_use."""
    tool_use_block = MagicMock()
    tool_use_block.type = "tool_use"
    tool_use_block.name = "save_document"
    tool_use_block.input = {"content": content}

    message = MagicMock(spec=anthropic.types.Message)
    message.content = [tool_use_block]
    return message


class TestBlueprintAgentRun:
    """Tests for BlueprintAgent.run()."""

    @pytest.mark.asyncio
    @patch("app.services.docs.blueprint_agent.document_ops")
    @patch("app.services.docs.blueprint_agent.repository_ops")
    async def test_run_creates_overview_when_missing(self, mock_repo_ops, mock_doc_ops):
        """Creates Project Overview when no overview exists."""
        mock_doc_ops.get_by_folder = AsyncMock(return_value=[])
        mock_repo_ops.get_github_repos_by_product = AsyncMock(return_value=[])

        agent = _make_agent()
        mock_response = _make_mock_claude_response("# Project Overview\n\nContent.")
        agent.client.messages.create = AsyncMock(return_value=mock_response)

        result = await agent.run()

        assert isinstance(result, BlueprintResult)
        assert result.created_count == 1
        # Verify the document was added to DB
        agent.db.add.assert_called()

    @pytest.mark.asyncio
    @patch("app.services.docs.blueprint_agent.document_ops")
    @patch("app.services.docs.blueprint_agent.repository_ops")
    async def test_run_skips_overview_if_exists(self, mock_repo_ops, mock_doc_ops):
        """Skips overview creation when one already exists."""
        existing_overview = MagicMock()
        existing_overview.title = "Project Overview"
        mock_doc_ops.get_by_folder = AsyncMock(return_value=[existing_overview])
        mock_repo_ops.get_github_repos_by_product = AsyncMock(return_value=[])

        agent = _make_agent()

        result = await agent.run()

        assert result.created_count == 0
        assert len(result.documents) == 1  # Just the existing one

    @pytest.mark.asyncio
    @patch("app.services.docs.blueprint_agent.document_ops")
    @patch("app.services.docs.blueprint_agent.repository_ops")
    async def test_run_creates_architecture_for_complex_project(self, mock_repo_ops, mock_doc_ops):
        """Creates Architecture doc when project has >50 files."""
        mock_doc_ops.get_by_folder = AsyncMock(return_value=[])
        # Need at least a repo so _fetch_repo_contexts returns something
        mock_repo = MagicMock()
        mock_repo.full_name = "org/repo"
        mock_repo.default_branch = "main"
        mock_repo.description = "A big repo"
        mock_repo_ops.get_github_repos_by_product = AsyncMock(return_value=[mock_repo])
        # Mock github_service.get_repo_context to return context with many files
        agent = _make_agent()
        agent.github_service.get_repo_context = AsyncMock(
            return_value=_make_repo_context(file_count=60)
        )
        mock_response = _make_mock_claude_response("# Architecture\n\nContent.")
        agent.client.messages.create = AsyncMock(return_value=mock_response)

        result = await agent.run()

        # Should create both overview + architecture for a complex project
        assert result.created_count == 2

    @pytest.mark.asyncio
    @patch("app.services.docs.blueprint_agent.document_ops")
    @patch("app.services.docs.blueprint_agent.repository_ops")
    async def test_run_handles_generation_failure_gracefully(self, mock_repo_ops, mock_doc_ops):
        """If Claude generation fails, other docs still complete."""
        mock_doc_ops.get_by_folder = AsyncMock(return_value=[])
        mock_repo_ops.get_github_repos_by_product = AsyncMock(return_value=[])

        agent = _make_agent()
        agent.client.messages.create = AsyncMock(
            side_effect=anthropic.APIError(
                message="Rate limited",
                request=MagicMock(),
                body=None,
            )
        )

        result = await agent.run()

        # Should still return a result, just with 0 created
        assert result.created_count == 0


class TestBlueprintComplexityDetection:
    """Tests for _is_complex_project."""

    def test_complex_with_many_files(self):
        """Project with >50 files is complex."""
        agent = _make_agent()
        ctx = _make_repo_context(file_count=60)

        assert agent._is_complex_project([ctx]) is True

    def test_complex_with_multiple_repos(self):
        """Project with >1 repo is complex."""
        agent = _make_agent()
        ctx1 = _make_repo_context(file_count=10)
        ctx2 = _make_repo_context(file_count=10)

        assert agent._is_complex_project([ctx1, ctx2]) is True

    def test_simple_project(self):
        """Small single-repo project is simple."""
        agent = _make_agent()
        ctx = _make_repo_context(file_count=20)

        assert agent._is_complex_project([ctx]) is False

    def test_no_repos_is_not_complex(self):
        """No repos means not complex."""
        agent = _make_agent()
        assert agent._is_complex_project([]) is False


class TestBlueprintToolSchema:
    """Tests for tool schema structure."""

    def test_tool_schema_has_save_document_name(self):
        """Tool schema uses save_document name."""
        agent = _make_agent()
        spec = DocumentSpec(
            title="Test",
            folder_path="blueprints",
            doc_type="blueprint",
            prompt_context="Generate test doc",
        )

        schema = agent._build_tool_schema(spec)

        assert schema["name"] == "save_document"
        assert "content" in schema["input_schema"]["required"]


class TestBlueprintResponseParsing:
    """Tests for _parse_response."""

    def test_parse_valid_tool_use_response(self):
        """Extracts content from save_document tool use."""
        agent = _make_agent()
        response = _make_mock_claude_response("# Overview\n\nHello world.")
        spec = DocumentSpec(
            title="Overview",
            folder_path="blueprints",
            doc_type="blueprint",
            prompt_context="Generate",
        )

        result = agent._parse_response(response, spec)

        assert result == "# Overview\n\nHello world."

    def test_parse_no_tool_use_returns_fallback(self):
        """Returns fallback when no tool_use block found."""
        agent = _make_agent()
        text_block = MagicMock()
        text_block.type = "text"
        text_block.text = "Sorry, I can't generate this."
        message = MagicMock(spec=anthropic.types.Message)
        message.content = [text_block]

        spec = DocumentSpec(
            title="My Doc",
            folder_path="blueprints",
            doc_type="blueprint",
            prompt_context="Generate",
        )

        result = agent._parse_response(message, spec)

        assert "My Doc" in result
        assert "failed" in result.lower()
