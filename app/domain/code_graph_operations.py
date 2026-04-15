"""Code graph operations — bulk insert/query for code nodes and edges.

Unlike user-owned resources, code graph data is scoped to a repository.
Access follows the repository → product RLS chain.
"""

import uuid as uuid_pkg

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.code_edge import CodeEdge, CodeEdgeType
from app.models.code_node import CodeNode, CodeNodeType


class CodeGraphOperations:
    """CRUD operations for the code knowledge graph (nodes + edges)."""

    # ------------------------------------------------------------------
    # Bulk write (used by indexing pipeline)
    # ------------------------------------------------------------------

    async def bulk_insert_nodes(
        self,
        db: AsyncSession,
        nodes: list[dict[str, object]],
    ) -> int:
        """Insert many nodes at once. Returns count inserted.

        Each dict should contain: repo_id, type, name, file_path,
        and optionally start_line, end_line, metadata_.
        """
        if not nodes:
            return 0
        db_objects = [CodeNode(**n) for n in nodes]
        db.add_all(db_objects)
        await db.flush()
        return len(db_objects)

    async def bulk_insert_edges(
        self,
        db: AsyncSession,
        edges: list[dict[str, object]],
    ) -> int:
        """Insert many edges at once. Returns count inserted.

        Each dict should contain: repo_id, source_node_id, target_node_id,
        type, and optionally metadata_.
        """
        if not edges:
            return 0
        db_objects = [CodeEdge(**e) for e in edges]
        db.add_all(db_objects)
        await db.flush()
        return len(db_objects)

    # ------------------------------------------------------------------
    # Delete (used before re-indexing)
    # ------------------------------------------------------------------

    async def delete_graph_for_repo(
        self,
        db: AsyncSession,
        repo_id: uuid_pkg.UUID,
    ) -> tuple[int, int]:
        """Delete all nodes and edges for a repository.

        Edges are deleted first (FK constraint), then nodes.
        Returns (nodes_deleted, edges_deleted).
        """
        # Edges first (they reference nodes)
        edge_result = await db.execute(
            delete(CodeEdge).where(CodeEdge.repo_id == repo_id)  # type: ignore[arg-type]
        )
        edges_deleted: int = edge_result.rowcount  # type: ignore[attr-defined]

        node_result = await db.execute(
            delete(CodeNode).where(CodeNode.repo_id == repo_id)  # type: ignore[arg-type]
        )
        nodes_deleted: int = node_result.rowcount  # type: ignore[attr-defined]

        await db.flush()
        return nodes_deleted, edges_deleted

    # ------------------------------------------------------------------
    # Read (used by API)
    # ------------------------------------------------------------------

    async def get_graph_for_repo(
        self,
        db: AsyncSession,
        repo_id: uuid_pkg.UUID,
        node_types: list[CodeNodeType] | None = None,
        edge_types: list[CodeEdgeType] | None = None,
    ) -> tuple[list[CodeNode], list[CodeEdge]]:
        """Fetch all nodes and edges for a repository.

        Optionally filter by node/edge type.
        """
        # Nodes
        node_stmt = select(CodeNode).where(CodeNode.repo_id == repo_id)  # type: ignore[arg-type]
        if node_types:
            node_stmt = node_stmt.where(CodeNode.type.in_(node_types))  # type: ignore[attr-defined]
        node_result = await db.execute(node_stmt)
        nodes = list(node_result.scalars().all())

        # Edges — if node_types filter is active, only return edges between
        # the returned nodes to keep the graph consistent
        edge_stmt = select(CodeEdge).where(CodeEdge.repo_id == repo_id)  # type: ignore[arg-type]
        if edge_types:
            edge_stmt = edge_stmt.where(CodeEdge.type.in_(edge_types))  # type: ignore[attr-defined]
        if node_types:
            node_ids = {n.id for n in nodes}
            if node_ids:
                edge_stmt = edge_stmt.where(
                    CodeEdge.source_node_id.in_(node_ids),  # type: ignore[attr-defined]
                    CodeEdge.target_node_id.in_(node_ids),  # type: ignore[attr-defined]
                )
            else:
                # No nodes match — return empty edges too
                return nodes, []
        edge_result = await db.execute(edge_stmt)
        edges = list(edge_result.scalars().all())

        return nodes, edges

    async def search_nodes_by_name(
        self,
        db: AsyncSession,
        repo_id: uuid_pkg.UUID,
        name_query: str,
        node_types: list[CodeNodeType] | None = None,
        limit: int = 50,
    ) -> list[CodeNode]:
        """Search for nodes by name (case-insensitive substring match).

        More efficient than fetching the full graph and filtering in Python.
        Uses the (repo_id, name) index.
        """
        stmt = select(CodeNode).where(
            CodeNode.repo_id == repo_id,  # type: ignore[arg-type]
            func.lower(CodeNode.name).contains(name_query.lower()),
        )
        if node_types:
            stmt = stmt.where(CodeNode.type.in_(node_types))  # type: ignore[attr-defined]
        stmt = stmt.limit(limit)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_node(
        self,
        db: AsyncSession,
        node_id: uuid_pkg.UUID,
    ) -> CodeNode | None:
        """Get a single node by ID."""
        result = await db.execute(select(CodeNode).where(CodeNode.id == node_id))  # type: ignore[arg-type]
        return result.scalar_one_or_none()

    async def get_neighbours(
        self,
        db: AsyncSession,
        node_id: uuid_pkg.UUID,
        depth: int = 1,
    ) -> tuple[list[CodeNode], list[CodeEdge]]:
        """Get nodes connected to the given node, up to `depth` hops.

        For depth=1, returns direct neighbours. For depth>1, uses iterative
        expansion (not recursive CTE) to keep complexity manageable.
        """
        visited_node_ids: set[uuid_pkg.UUID] = {node_id}
        all_edges: list[CodeEdge] = []
        frontier = {node_id}

        for _ in range(depth):
            if not frontier:
                break

            # Outgoing edges
            out_stmt = select(CodeEdge).where(CodeEdge.source_node_id.in_(frontier))  # type: ignore[attr-defined]
            out_result = await db.execute(out_stmt)
            out_edges = list(out_result.scalars().all())

            # Incoming edges
            in_stmt = select(CodeEdge).where(CodeEdge.target_node_id.in_(frontier))  # type: ignore[attr-defined]
            in_result = await db.execute(in_stmt)
            in_edges = list(in_result.scalars().all())

            new_frontier: set[uuid_pkg.UUID] = set()
            for e in out_edges + in_edges:
                all_edges.append(e)
                for nid in (e.source_node_id, e.target_node_id):
                    if nid not in visited_node_ids:
                        visited_node_ids.add(nid)
                        new_frontier.add(nid)

            frontier = new_frontier

        # Fetch all discovered nodes
        if visited_node_ids:
            node_stmt = select(CodeNode).where(CodeNode.id.in_(visited_node_ids))  # type: ignore[attr-defined]
            node_result = await db.execute(node_stmt)
            nodes = list(node_result.scalars().all())
        else:
            nodes = []

        # Deduplicate edges
        seen_edge_ids: set[uuid_pkg.UUID] = set()
        unique_edges: list[CodeEdge] = []
        for e in all_edges:
            if e.id not in seen_edge_ids:
                seen_edge_ids.add(e.id)
                unique_edges.append(e)

        return nodes, unique_edges

    async def count_for_repo(
        self,
        db: AsyncSession,
        repo_id: uuid_pkg.UUID,
    ) -> tuple[int, int]:
        """Return (node_count, edge_count) for a repository."""
        node_count = await db.execute(
            select(func.count(CodeNode.id)).where(CodeNode.repo_id == repo_id)  # type: ignore[arg-type]
        )
        edge_count = await db.execute(
            select(func.count(CodeEdge.id)).where(CodeEdge.repo_id == repo_id)  # type: ignore[arg-type]
        )
        return (node_count.scalar() or 0, edge_count.scalar() or 0)


code_graph_ops = CodeGraphOperations()
