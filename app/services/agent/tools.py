"""On-demand file access tools for the PM Agent.

Defines tools that allow the agent to interactively read files and
explore repository structure during a conversation. These complement
the pre-loaded context from Phases 1-2 by letting the agent fetch
specific files on demand.
"""

import logging
from collections.abc import Sequence
from typing import Any

from app.services.github import GitHubService

logger = logging.getLogger(__name__)

# Limits
_MAX_FILE_CONTENT_CHARS = 50_000  # ~12K tokens
_MAX_LISTED_FILES = 200

# Tool definitions for the Anthropic API
AGENT_TOOLS: list[dict[str, Any]] = [
    {
        "name": "read_file",
        "description": (
            "Read the contents of a specific file from a connected GitHub repository. "
            "Use this when the user asks about specific code, implementation details, "
            "or when you need to examine a file not already included in the pre-loaded context."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repository": {
                    "type": "string",
                    "description": (
                        "Full repository name (owner/repo), e.g. 'acme/backend'. "
                        "Must be one of the connected repositories listed in the project context."
                    ),
                },
                "file_path": {
                    "type": "string",
                    "description": (
                        "Path to the file within the repository, e.g. 'src/auth/middleware.py'"
                    ),
                },
            },
            "required": ["repository", "file_path"],
        },
    },
    {
        "name": "list_files",
        "description": (
            "List files in a connected GitHub repository, optionally filtered by directory path. "
            "Use this to explore the repository structure when looking for specific files "
            "or understanding the project layout."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repository": {
                    "type": "string",
                    "description": (
                        "Full repository name (owner/repo), e.g. 'acme/backend'. "
                        "Must be one of the connected repositories listed in the project context."
                    ),
                },
                "path": {
                    "type": "string",
                    "description": (
                        "Optional directory path to filter results, e.g. 'src/auth'. "
                        "If omitted, lists all files in the repository."
                    ),
                },
            },
            "required": ["repository"],
        },
    },
]


class AgentToolExecutor:
    """Executes tool calls for the PM Agent.

    Provides read_file and list_files capabilities using the existing
    GitHubService, scoped to the product's connected repositories.
    """

    def __init__(self, github_token: str, repos: Sequence[object]) -> None:
        self._gh = GitHubService(github_token)
        # Build lookup: full_name -> repo object
        self._repos: dict[str, object] = {}
        for r in repos:
            name = getattr(r, "full_name", "")
            if name and "/" in name:
                self._repos[name] = r

    async def execute(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        """Execute a tool call and return the result as a string."""
        try:
            if tool_name == "read_file":
                return await self._read_file(tool_input)
            elif tool_name == "list_files":
                return await self._list_files(tool_input)
            else:
                return f"Unknown tool: {tool_name}"
        except Exception:
            logger.warning("Tool execution failed: %s", tool_name, exc_info=True)
            return f"Error executing {tool_name}. The operation could not be completed."

    async def _read_file(self, tool_input: dict[str, Any]) -> str:
        """Read a single file from a connected repository."""
        repo_name = tool_input.get("repository", "")
        file_path = tool_input.get("file_path", "")

        if repo_name not in self._repos:
            available = ", ".join(self._repos.keys()) or "none"
            return (
                f"Repository '{repo_name}' is not connected to this project. "
                f"Available repositories: {available}"
            )

        repo = self._repos[repo_name]
        owner, name = repo_name.split("/", 1)
        branch = getattr(repo, "default_branch", None) or "main"

        result = await self._gh.get_file_content(owner, name, file_path, branch)
        if not result:
            return f"File not found: {file_path} (branch: {branch})"

        content = result.content
        if len(content) > _MAX_FILE_CONTENT_CHARS:
            content = (
                content[:_MAX_FILE_CONTENT_CHARS] + "\n\n... (truncated — file exceeds 50K chars)"
            )

        return f"**{file_path}** ({result.size} bytes)\n```\n{content}\n```"

    async def _list_files(self, tool_input: dict[str, Any]) -> str:
        """List files in a connected repository, optionally filtered by path."""
        repo_name = tool_input.get("repository", "")
        path_prefix = tool_input.get("path", "")

        if repo_name not in self._repos:
            available = ", ".join(self._repos.keys()) or "none"
            return (
                f"Repository '{repo_name}' is not connected to this project. "
                f"Available repositories: {available}"
            )

        repo = self._repos[repo_name]
        owner, name = repo_name.split("/", 1)
        branch = getattr(repo, "default_branch", None) or "main"

        tree = await self._gh.get_repo_tree(owner, name, branch=branch)
        if not tree or not tree.files:
            return "Unable to fetch repository file tree, or repository is empty."

        files = tree.files
        if path_prefix:
            prefix = path_prefix.rstrip("/") + "/"
            files = [f for f in files if f.startswith(prefix) or f == path_prefix]

        if not files:
            hint = f" under '{path_prefix}'" if path_prefix else ""
            return f"No files found{hint}."

        total = len(files)
        display = files[:_MAX_LISTED_FILES]
        result = "\n".join(display)
        if total > _MAX_LISTED_FILES:
            result += (
                f"\n\n... and {total - _MAX_LISTED_FILES} more files "
                f"(showing first {_MAX_LISTED_FILES})"
            )

        hint = f" under '{path_prefix}'" if path_prefix else ""
        return f"{total} files{hint}:\n{result}"
