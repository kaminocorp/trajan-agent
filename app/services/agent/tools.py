"""On-demand file access and code graph tools for the PM Agent.

Defines tools that allow the agent to interactively read files,
explore repository structure, and query the code knowledge graph
during a conversation.
"""

import logging
import uuid as uuid_pkg
from collections.abc import Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.github import GitHubService

logger = logging.getLogger(__name__)

# Limits
_MAX_FILE_CONTENT_CHARS = 50_000  # ~12K tokens
_MAX_LISTED_FILES = 200
_MAX_GRAPH_QUERY_NODES = 100
_MAX_CALL_CHAIN_DEPTH = 5

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

# Code graph tools — only included when graph data is available
CODE_GRAPH_TOOLS: list[dict[str, Any]] = [
    {
        "name": "query_code_graph",
        "description": (
            "Search the code knowledge graph for symbols (functions, classes, methods, "
            "interfaces) and their relationships. Use this to find where things are "
            "defined, what calls what, and how modules connect. Returns matching nodes "
            "with their file paths, line numbers, and connections."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "repository": {
                    "type": "string",
                    "description": "Full repository name (owner/repo).",
                },
                "name": {
                    "type": "string",
                    "description": (
                        "Symbol name to search for (case-insensitive substring match). "
                        "E.g. 'handlePayment', 'UserService', 'authenticate'."
                    ),
                },
                "node_type": {
                    "type": "string",
                    "description": (
                        "Optional filter by node type: folder, file, class, function, "
                        "method, interface, type, enum, import, variable."
                    ),
                },
            },
            "required": ["repository", "name"],
        },
    },
    {
        "name": "get_symbol_context",
        "description": (
            "Get the source code context for a specific symbol in the code graph. "
            "Returns the file content around the symbol with its incoming and outgoing "
            "connections. Use this after query_code_graph to inspect a specific result."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "node_id": {
                    "type": "string",
                    "description": "The UUID of a code graph node (from query_code_graph results).",
                },
            },
            "required": ["node_id"],
        },
    },
    {
        "name": "trace_call_chain",
        "description": (
            "Trace the call chain from a function/method through the codebase. "
            "Returns all functions that the target calls (outgoing) and all functions "
            "that call the target (incoming), up to the specified depth. Use this to "
            "understand execution flows and impact analysis."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "node_id": {
                    "type": "string",
                    "description": (
                        "The UUID of a function/method node to trace from "
                        "(from query_code_graph results)."
                    ),
                },
                "depth": {
                    "type": "integer",
                    "description": (
                        "How many hops to follow. 1 = direct callers/callees, "
                        "2 = two levels deep, etc. Default 2, max 5."
                    ),
                },
            },
            "required": ["node_id"],
        },
    },
]


class AgentToolExecutor:
    """Executes tool calls for the PM Agent.

    Provides file access tools (read_file, list_files) using GitHubService,
    and code graph tools (query_code_graph, get_symbol_context, trace_call_chain)
    using the code graph operations layer.
    """

    def __init__(
        self,
        github_token: str,
        repos: Sequence[object],
        db: AsyncSession | None = None,
    ) -> None:
        self._gh = GitHubService(github_token)
        self._db = db
        # Build lookup: full_name -> repo object
        self._repos: dict[str, object] = {}
        for r in repos:
            name = getattr(r, "full_name", "")
            if name and "/" in name:
                self._repos[name] = r

        # Build reverse lookup: full_name -> repo_id for graph queries
        self._repo_ids: dict[str, uuid_pkg.UUID] = {}
        for r in repos:
            name = getattr(r, "full_name", "")
            rid = getattr(r, "id", None)
            if name and "/" in name and rid:
                self._repo_ids[name] = rid

    async def execute(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        """Execute a tool call and return the result as a string."""
        try:
            if tool_name == "read_file":
                return await self._read_file(tool_input)
            elif tool_name == "list_files":
                return await self._list_files(tool_input)
            elif tool_name == "query_code_graph":
                return await self._query_code_graph(tool_input)
            elif tool_name == "get_symbol_context":
                return await self._get_symbol_context(tool_input)
            elif tool_name == "trace_call_chain":
                return await self._trace_call_chain(tool_input)
            else:
                return f"Unknown tool: {tool_name}"
        except Exception:
            logger.warning("Tool execution failed: %s", tool_name, exc_info=True)
            return f"Error executing {tool_name}. The operation could not be completed."

    # ------------------------------------------------------------------
    # File access tools
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Code graph tools
    # ------------------------------------------------------------------

    async def _query_code_graph(self, tool_input: dict[str, Any]) -> str:
        """Search the code knowledge graph for symbols by name."""
        if not self._db:
            return "Code graph is not available (no database session)."

        from app.domain.code_graph_operations import code_graph_ops
        from app.models.code_node import CodeNodeType

        repo_name = tool_input.get("repository", "")
        name_query = tool_input.get("name", "")
        node_type_str = tool_input.get("node_type")

        if repo_name not in self._repo_ids:
            available = ", ".join(self._repo_ids.keys()) or "none"
            return (
                f"Repository '{repo_name}' is not connected or not indexed. "
                f"Available: {available}"
            )

        repo_id = self._repo_ids[repo_name]

        # Build type filter if specified
        node_types = None
        if node_type_str:
            try:
                node_types = [CodeNodeType(node_type_str)]
            except ValueError:
                return f"Invalid node type: '{node_type_str}'. Valid types: folder, file, class, function, method, interface, type, enum, import, variable."

        # Search by name in the database (efficient — uses index, not full scan)
        matching = await code_graph_ops.search_nodes_by_name(
            self._db,
            repo_id,
            name_query,
            node_types=node_types,
            limit=_MAX_GRAPH_QUERY_NODES,
        )

        if not matching:
            return f"No symbols matching '{name_query}' found in {repo_name}."

        # Format results with code-ref links
        lines: list[str] = [
            f"Found {len(matching)} symbols matching '{name_query}' in {repo_name}:"
        ]

        for n in matching:
            line_info = ""
            if n.start_line:
                line_info = f" (line {n.start_line}"
                if n.end_line and n.end_line != n.start_line:
                    line_info += f"-{n.end_line}"
                line_info += ")"

            # Include node ID so the agent can use it with get_symbol_context
            lines.append(
                f"- **{n.name}** [{n.type.value}] — "
                f"`{n.file_path}`{line_info} "
                f"[code-ref://{n.file_path}:{n.start_line or 0}] "
                f"(id: {n.id})"
            )

        return "\n".join(lines)

    async def _get_symbol_context(self, tool_input: dict[str, Any]) -> str:
        """Get detailed context for a specific node including connections."""
        if not self._db:
            return "Code graph is not available (no database session)."

        from app.domain.code_graph_operations import code_graph_ops

        node_id_str = tool_input.get("node_id", "")
        try:
            node_id = uuid_pkg.UUID(node_id_str)
        except (ValueError, AttributeError):
            return f"Invalid node ID: '{node_id_str}'. Use an ID from query_code_graph results."

        node = await code_graph_ops.get_node(self._db, node_id)
        if not node:
            return f"Node not found: {node_id_str}"

        # Verify node belongs to an accessible repository
        accessible_repo_ids = set(self._repo_ids.values())
        if node.repo_id not in accessible_repo_ids:
            return f"Node not found: {node_id_str}"

        # Get direct neighbours
        neighbour_nodes, neighbour_edges = await code_graph_ops.get_neighbours(
            self._db, node_id, depth=1
        )

        # Build node lookup
        node_map = {n.id: n for n in neighbour_nodes}

        # Format result
        lines: list[str] = [
            f"## {node.name} [{node.type.value}]",
            f"**File:** `{node.file_path}`",
        ]
        if node.start_line:
            end = f"-{node.end_line}" if node.end_line and node.end_line != node.start_line else ""
            lines.append(f"**Lines:** {node.start_line}{end}")
            lines.append(f"[code-ref://{node.file_path}:{node.start_line}]")

        # Outgoing connections
        outgoing = [e for e in neighbour_edges if e.source_node_id == node_id]
        if outgoing:
            lines.append(f"\n**Outgoing ({len(outgoing)}):**")
            for e in outgoing[:30]:
                target = node_map.get(e.target_node_id)
                if target:
                    lines.append(
                        f"- {e.type.value} → **{target.name}** "
                        f"[{target.type.value}] `{target.file_path}`"
                    )

        # Incoming connections
        incoming = [e for e in neighbour_edges if e.target_node_id == node_id]
        if incoming:
            lines.append(f"\n**Incoming ({len(incoming)}):**")
            for e in incoming[:30]:
                source = node_map.get(e.source_node_id)
                if source:
                    lines.append(
                        f"- {e.type.value} ← **{source.name}** "
                        f"[{source.type.value}] `{source.file_path}`"
                    )

        return "\n".join(lines)

    async def _trace_call_chain(self, tool_input: dict[str, Any]) -> str:
        """Trace call chains from a function/method node."""
        if not self._db:
            return "Code graph is not available (no database session)."

        from app.domain.code_graph_operations import code_graph_ops

        node_id_str = tool_input.get("node_id", "")
        depth = min(tool_input.get("depth", 2), _MAX_CALL_CHAIN_DEPTH)

        try:
            node_id = uuid_pkg.UUID(node_id_str)
        except (ValueError, AttributeError):
            return f"Invalid node ID: '{node_id_str}'. Use an ID from query_code_graph results."

        node = await code_graph_ops.get_node(self._db, node_id)
        if not node:
            return f"Node not found: {node_id_str}"

        # Verify node belongs to an accessible repository
        accessible_repo_ids = set(self._repo_ids.values())
        if node.repo_id not in accessible_repo_ids:
            return f"Node not found: {node_id_str}"

        # Get the subgraph around this node
        nodes, edges = await code_graph_ops.get_neighbours(self._db, node_id, depth=depth)
        node_map = {n.id: n for n in nodes}

        # Filter to only "calls" edges for the trace
        call_edges = [e for e in edges if e.type.value == "calls"]

        # Build call chain: who this function calls (downstream)
        def trace_downstream(nid: uuid_pkg.UUID, depth_remaining: int) -> list[str]:
            if depth_remaining <= 0:
                return []
            result: list[str] = []
            for e in call_edges:
                if e.source_node_id == nid:
                    target = node_map.get(e.target_node_id)
                    if target:
                        indent = "  " * (_MAX_CALL_CHAIN_DEPTH - depth_remaining + 1)
                        result.append(
                            f"{indent}→ **{target.name}** [{target.type.value}] "
                            f"`{target.file_path}`"
                        )
                        result.extend(
                            trace_downstream(e.target_node_id, depth_remaining - 1)
                        )
            return result

        # Build call chain: who calls this function (upstream)
        def trace_upstream(nid: uuid_pkg.UUID, depth_remaining: int) -> list[str]:
            if depth_remaining <= 0:
                return []
            result: list[str] = []
            for e in call_edges:
                if e.target_node_id == nid:
                    source = node_map.get(e.source_node_id)
                    if source:
                        indent = "  " * (_MAX_CALL_CHAIN_DEPTH - depth_remaining + 1)
                        result.append(
                            f"{indent}← **{source.name}** [{source.type.value}] "
                            f"`{source.file_path}`"
                        )
                        result.extend(
                            trace_upstream(e.source_node_id, depth_remaining - 1)
                        )
            return result

        lines: list[str] = [
            f"## Call chain for {node.name} [{node.type.value}]",
            f"`{node.file_path}`"
            + (f" (line {node.start_line})" if node.start_line else ""),
            f"[code-ref://{node.file_path}:{node.start_line or 0}]",
        ]

        downstream = trace_downstream(node_id, depth)
        upstream = trace_upstream(node_id, depth)

        if downstream:
            lines.append(f"\n**Calls (downstream, {depth} hops):**")
            lines.extend(downstream)
        else:
            lines.append("\n**Calls:** none (leaf function)")

        if upstream:
            lines.append(f"\n**Called by (upstream, {depth} hops):**")
            lines.extend(upstream)
        else:
            lines.append("\n**Called by:** none (entry point or unreferenced)")

        lines.append(
            f"\nTotal: {len(call_edges)} call edges in the {depth}-hop subgraph "
            f"({len(nodes)} nodes)"
        )

        return "\n".join(lines)
