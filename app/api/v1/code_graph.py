"""Code Map API: codebase indexing and knowledge graph retrieval.

Endpoints for triggering tree-sitter indexing of a repository and
fetching the resulting code graph (nodes + edges) for frontend rendering.
"""

import logging
import uuid as uuid_pkg
from collections import deque

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    check_product_editor_access,
    check_product_viewer_access,
    get_current_user,
    get_db_with_rls,
)
from app.api.v1.progress.utils import resolve_github_token
from app.domain import repository_ops
from app.domain.code_graph_operations import code_graph_ops
from app.models.code_edge import CodeEdge, CodeEdgeType
from app.models.code_node import CodeNode, CodeNodeType
from app.models.repository import IndexingStatus
from app.models.user import User
from app.schemas.code_graph import (
    CodeEdgeRead,
    CodeGraphResponse,
    CodeNodeRead,
    ExecutionFlow,
    ExecutionFlowsResponse,
    FileContentResponse,
    FlowStep,
    IndexingStatusResponse,
    TriggerIndexingResponse,
)

router = APIRouter(prefix="/code-graph", tags=["code-graph"])
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _serialize_node(n: CodeNode) -> CodeNodeRead:
    return CodeNodeRead(
        id=str(n.id),
        repo_id=str(n.repo_id),
        type=n.type.value if hasattr(n.type, "value") else str(n.type),
        name=n.name,
        file_path=n.file_path,
        start_line=n.start_line,
        end_line=n.end_line,
        metadata=n.metadata_,
    )


def _serialize_edge(e: CodeEdge) -> CodeEdgeRead:
    return CodeEdgeRead(
        id=str(e.id),
        repo_id=str(e.repo_id),
        source_node_id=str(e.source_node_id),
        target_node_id=str(e.target_node_id),
        type=e.type.value if hasattr(e.type, "value") else str(e.type),
        metadata=e.metadata_,
    )


# ---------------------------------------------------------------------------
# Trigger Indexing
# ---------------------------------------------------------------------------


@router.post(
    "/repositories/{repository_id}/index",
    response_model=TriggerIndexingResponse,
)
async def trigger_indexing(
    repository_id: uuid_pkg.UUID,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> TriggerIndexingResponse:
    """Trigger codebase indexing for a repository.

    Clones the repo, parses source files with tree-sitter, and stores
    the resulting knowledge graph. Runs as a background task.

    Requires editor access to the repository's product.
    """
    repo = await repository_ops.get(db, id=repository_id)
    if not repo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repository not found",
        )

    if not repo.product_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Repository is not associated with a product",
        )

    # Check editor access
    await check_product_editor_access(db, repo.product_id, current_user.id)

    # Check if already indexing
    if repo.indexing_status == IndexingStatus.INDEXING.value:
        return TriggerIndexingResponse(
            status="already_running",
            message="Indexing is already in progress for this repository.",
        )

    # Resolve GitHub token
    github_token = await resolve_github_token(
        db, current_user, repo.product_id, repo_full_name=repo.full_name
    )
    if not github_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No GitHub access configured. Install the GitHub App, "
            "add a Personal Access Token, or link repos with a fine-grained token.",
        )

    # Mark as pending before background task starts
    repo.indexing_status = IndexingStatus.PENDING.value
    repo.index_error = None
    await db.flush()
    await db.commit()

    # Start background indexing
    background_tasks.add_task(
        _run_indexing,
        repo_id=str(repository_id),
        github_token=github_token,
    )

    return TriggerIndexingResponse(
        status="started",
        message="Codebase indexing started. Poll /index-status for progress.",
    )


# ---------------------------------------------------------------------------
# Indexing Status
# ---------------------------------------------------------------------------


@router.get(
    "/repositories/{repository_id}/index-status",
    response_model=IndexingStatusResponse,
)
async def get_indexing_status(
    repository_id: uuid_pkg.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> IndexingStatusResponse:
    """Get current indexing status for a repository.

    Requires viewer access to the repository's product.
    """
    repo = await repository_ops.get(db, id=repository_id)
    if not repo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repository not found",
        )

    if repo.product_id:
        await check_product_viewer_access(db, repo.product_id, current_user.id)

    node_count, edge_count = await code_graph_ops.count_for_repo(db, repository_id)

    return IndexingStatusResponse(
        repo_id=str(repository_id),
        indexing_status=repo.indexing_status,
        last_indexed_at=repo.last_indexed_at,
        index_error=repo.index_error,
        node_count=node_count,
        edge_count=edge_count,
    )


# ---------------------------------------------------------------------------
# Fetch Graph
# ---------------------------------------------------------------------------


@router.get(
    "/repositories/{repository_id}",
    response_model=CodeGraphResponse,
)
async def get_code_graph(
    repository_id: uuid_pkg.UUID,
    node_types: str | None = Query(
        None, description="Comma-separated node types to filter (e.g. 'file,class,function')"
    ),
    edge_types: str | None = Query(
        None, description="Comma-separated edge types to filter (e.g. 'calls,imports')"
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> CodeGraphResponse:
    """Fetch the code knowledge graph for a repository.

    Returns all nodes and edges, optionally filtered by type.
    Requires viewer access to the repository's product.
    """
    repo = await repository_ops.get(db, id=repository_id)
    if not repo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repository not found",
        )

    if repo.product_id:
        await check_product_viewer_access(db, repo.product_id, current_user.id)

    # Parse filter params
    nt_filter = None
    if node_types:
        try:
            nt_filter = [CodeNodeType(t.strip()) for t in node_types.split(",")]
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid node type: {e}",
            ) from None

    et_filter = None
    if edge_types:
        try:
            et_filter = [CodeEdgeType(t.strip()) for t in edge_types.split(",")]
        except ValueError as e:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid edge type: {e}",
            ) from None

    nodes, edges = await code_graph_ops.get_graph_for_repo(
        db, repository_id, node_types=nt_filter, edge_types=et_filter
    )

    return CodeGraphResponse(
        repo_id=str(repository_id),
        nodes=[_serialize_node(n) for n in nodes],
        edges=[_serialize_edge(e) for e in edges],
        node_count=len(nodes),
        edge_count=len(edges),
    )


# ---------------------------------------------------------------------------
# Neighbours (subgraph around a node)
# ---------------------------------------------------------------------------


@router.get(
    "/nodes/{node_id}/neighbours",
    response_model=CodeGraphResponse,
)
async def get_node_neighbours(
    node_id: uuid_pkg.UUID,
    depth: int = Query(1, ge=1, le=5, description="Hops from the selected node"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> CodeGraphResponse:
    """Get the subgraph around a specific node (up to `depth` hops).

    Requires viewer access to the node's repository's product.
    """
    node = await code_graph_ops.get_node(db, node_id)
    if not node:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Code node not found",
        )

    # Check access via repo → product
    repo = await repository_ops.get(db, id=node.repo_id)
    if repo and repo.product_id:
        await check_product_viewer_access(db, repo.product_id, current_user.id)

    nodes, edges = await code_graph_ops.get_neighbours(db, node_id, depth=depth)

    return CodeGraphResponse(
        repo_id=str(node.repo_id),
        nodes=[_serialize_node(n) for n in nodes],
        edges=[_serialize_edge(e) for e in edges],
        node_count=len(nodes),
        edge_count=len(edges),
    )


# ---------------------------------------------------------------------------
# Execution Flows (auto-detected entry points + call traces)
# ---------------------------------------------------------------------------

# File path patterns that suggest entry points
_ENTRY_POINT_PATH_PATTERNS = (
    "/api/",
    "/routes/",
    "/handlers/",
    "/views/",
    "/controllers/",
    "/endpoints/",
    "/pages/",
    "/commands/",
)

# Function name patterns that suggest entry points
_ENTRY_POINT_NAME_PATTERNS = (
    "main",
    "handler",
    "handle_",
    "on_",
    "listen",
    "serve",
    "run",
    "execute",
    "dispatch",
)

_MAX_FLOWS = 20
_MAX_FLOW_DEPTH = 8


@router.get(
    "/repositories/{repository_id}/flows",
    response_model=ExecutionFlowsResponse,
)
async def get_execution_flows(
    repository_id: uuid_pkg.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> ExecutionFlowsResponse:
    """Detect entry points and trace execution flows.

    Entry points are identified by:
    1. Functions/methods with NO incoming `calls` edges but WITH outgoing `calls` edges
    2. Functions in API/route/handler directories
    3. Functions named after common entry point patterns

    Each entry point's call chain is traced and returned as a Mermaid flowchart.
    """
    repo = await repository_ops.get(db, id=repository_id)
    if not repo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repository not found",
        )

    if repo.product_id:
        await check_product_viewer_access(db, repo.product_id, current_user.id)

    # Fetch the full graph
    nodes, edges = await code_graph_ops.get_graph_for_repo(db, repository_id)

    if not nodes:
        return ExecutionFlowsResponse(
            repo_id=str(repository_id), flows=[], entry_point_count=0
        )

    # Build lookup structures
    node_map = {n.id: n for n in nodes}
    # Outgoing calls: source -> [target_ids]
    outgoing_calls: dict[uuid_pkg.UUID, list[uuid_pkg.UUID]] = {}
    # Track which nodes have incoming calls
    has_incoming_call: set[uuid_pkg.UUID] = set()

    for e in edges:
        etype = e.type.value if hasattr(e.type, "value") else str(e.type)
        if etype == "calls":
            outgoing_calls.setdefault(e.source_node_id, []).append(e.target_node_id)
            has_incoming_call.add(e.target_node_id)

    # Detect entry points
    entry_points: list[CodeNode] = []
    for n in nodes:
        ntype = n.type.value if hasattr(n.type, "value") else str(n.type)
        if ntype not in ("function", "method"):
            continue

        is_entry = False

        # Heuristic 1: has outgoing calls but no incoming calls (topological entry)
        has_outgoing = n.id in outgoing_calls
        has_incoming = n.id in has_incoming_call
        if has_outgoing and not has_incoming:
            is_entry = True

        # Heuristic 2: file path matches entry point patterns
        if not is_entry and n.file_path:
            path_lower = n.file_path.lower()
            if any(p in path_lower for p in _ENTRY_POINT_PATH_PATTERNS):
                is_entry = True

        # Heuristic 3: name matches entry point patterns
        if not is_entry and n.name:
            name_lower = n.name.lower()
            if any(name_lower.startswith(p) or name_lower == p for p in _ENTRY_POINT_NAME_PATTERNS):
                is_entry = True

        if is_entry:
            entry_points.append(n)

    # Sort by number of outgoing calls (most connected first)
    entry_points.sort(
        key=lambda n: len(outgoing_calls.get(n.id, [])),
        reverse=True,
    )
    entry_points = entry_points[:_MAX_FLOWS]

    # Trace flows and build Mermaid diagrams
    flows: list[ExecutionFlow] = []
    for ep in entry_points:
        steps: list[FlowStep] = []
        visited: set[uuid_pkg.UUID] = set()
        bfs_queue: deque[tuple[uuid_pkg.UUID, int]] = deque([(ep.id, 0)])
        visited.add(ep.id)

        while bfs_queue:
            current_id, depth = bfs_queue.popleft()
            if depth > _MAX_FLOW_DEPTH:
                continue
            current = node_map.get(current_id)
            if not current:
                continue

            if current_id != ep.id:
                steps.append(FlowStep(
                    node_id=str(current.id),
                    name=current.name,
                    type=current.type.value if hasattr(current.type, "value") else str(current.type),
                    file_path=current.file_path,
                    start_line=current.start_line,
                ))

            for target_id in outgoing_calls.get(current_id, []):
                if target_id not in visited:
                    visited.add(target_id)
                    bfs_queue.append((target_id, depth + 1))

        # Build Mermaid flowchart
        mermaid_lines = ["graph TD"]
        ep_label = _mermaid_safe(ep.name)
        mermaid_lines.append(f'  EP["{ep_label}"]:::entry')

        for i, step in enumerate(steps[:15]):  # Limit diagram size
            step_label = _mermaid_safe(step.name)
            node_key = f"S{i}"
            mermaid_lines.append(f'  {node_key}["{step_label}"]')

        # Add edges
        for target_id in outgoing_calls.get(ep.id, []):
            target = node_map.get(target_id)
            if target:
                idx = next(
                    (i for i, s in enumerate(steps[:15]) if s.node_id == str(target_id)),
                    None,
                )
                if idx is not None:
                    mermaid_lines.append(f"  EP --> S{idx}")

        for i, step in enumerate(steps[:15]):
            step_uuid = uuid_pkg.UUID(step.node_id)
            for target_id in outgoing_calls.get(step_uuid, []):
                idx2 = next(
                    (j for j, s in enumerate(steps[:15]) if s.node_id == str(target_id)),
                    None,
                )
                if idx2 is not None:
                    mermaid_lines.append(f"  S{i} --> S{idx2}")

        mermaid_lines.append("  classDef entry fill:#c2410c,color:#fff,stroke:#c2410c")

        flows.append(ExecutionFlow(
            entry_point=FlowStep(
                node_id=str(ep.id),
                name=ep.name,
                type=ep.type.value if hasattr(ep.type, "value") else str(ep.type),
                file_path=ep.file_path,
                start_line=ep.start_line,
            ),
            steps=steps,
            mermaid="\n".join(mermaid_lines),
        ))

    return ExecutionFlowsResponse(
        repo_id=str(repository_id),
        flows=flows,
        entry_point_count=len(entry_points),
    )


def _mermaid_safe(text: str) -> str:
    """Escape text for Mermaid node labels."""
    return text.replace('"', "'").replace("\n", " ")[:40]


# ---------------------------------------------------------------------------
# File Content (on-demand from GitHub)
# ---------------------------------------------------------------------------


@router.get(
    "/repositories/{repository_id}/file-content",
    response_model=FileContentResponse,
)
async def get_file_content(
    repository_id: uuid_pkg.UUID,
    path: str = Query(..., description="Relative file path within the repository"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> FileContentResponse:
    """Fetch source code for a file from GitHub.

    Uses the GitHub API on-demand (no caching). Returns the decoded
    text content with language detection from the file extension.

    Requires viewer access to the repository's product.
    """
    repo = await repository_ops.get(db, id=repository_id)
    if not repo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repository not found",
        )

    if not repo.product_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Repository is not associated with a product.",
        )

    await check_product_viewer_access(db, repo.product_id, current_user.id)

    # Resolve GitHub token
    github_token = await resolve_github_token(
        db, current_user, repo.product_id, repo_full_name=repo.full_name
    )
    if not github_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No GitHub access configured.",
        )

    # Parse owner/repo from the repository URL
    full_name = repo.full_name or ""
    if not full_name and repo.url:
        # Extract from URL: https://github.com/owner/repo
        parts = repo.url.rstrip("/").split("/")
        if len(parts) >= 2:
            full_name = f"{parts[-2]}/{parts[-1]}"

    if "/" not in full_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot determine repository owner/name.",
        )

    owner, repo_name = full_name.split("/", 1)
    branch = repo.default_branch or "main"

    from app.services.github import GitHubService

    gh = GitHubService(token=github_token)
    file_data = await gh.get_file_content(
        owner=owner,
        repo=repo_name,
        path=path,
        branch=branch,
        max_size=500_000,  # 500KB — matches indexer limit
    )

    if not file_data:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"File not found: {path}",
        )

    # Detect language from file extension
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    ext_to_lang: dict[str, str] = {
        "py": "python",
        "ts": "typescript",
        "tsx": "tsx",
        "js": "javascript",
        "jsx": "jsx",
        "go": "go",
        "rs": "rust",
        "java": "java",
        "rb": "ruby",
        "sh": "bash",
        "yml": "yaml",
        "yaml": "yaml",
        "json": "json",
        "md": "markdown",
        "css": "css",
        "scss": "scss",
        "html": "html",
        "sql": "sql",
        "toml": "toml",
        "cfg": "ini",
        "ini": "ini",
        "xml": "xml",
        "dockerfile": "dockerfile",
    }
    language = ext_to_lang.get(ext)

    return FileContentResponse(
        file_path=path,
        content=file_data.content,
        language=language,
        size=file_data.size,
    )


# ---------------------------------------------------------------------------
# Background task
# ---------------------------------------------------------------------------


async def _run_indexing(
    repo_id: str,
    github_token: str,
) -> None:
    """Background task for codebase indexing.

    Uses a fresh DB session (same pattern as changelog generation).
    """
    from app.core.database import async_session_maker
    from app.services.codebase import CodebaseIndexer

    async with async_session_maker() as db:
        try:
            indexer = CodebaseIndexer(
                repo_id=uuid_pkg.UUID(repo_id),
                github_token=github_token,
            )
            result = await indexer.run(db)

            logger.info(
                f"Indexing background task completed for {repo_id}: "
                f"{result.nodes_created} nodes, {result.edges_created} edges"
            )
            if result.errors:
                logger.warning(f"Indexing had errors: {result.errors}")

        except Exception as e:
            logger.error(f"Indexing background task failed for {repo_id}: {e}")
