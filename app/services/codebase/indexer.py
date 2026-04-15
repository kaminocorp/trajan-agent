"""Codebase indexer — orchestrates clone → parse → store pipeline.

Entry point for the Code Map indexing feature. Clones the repository,
walks the file tree, parses each supported file with tree-sitter, and
stores the resulting nodes/edges in PostgreSQL.
"""

import asyncio
import logging
import shutil
import tempfile
import uuid as uuid_pkg
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.code_graph_operations import code_graph_ops
from app.domain.repository_operations import repository_ops
from app.models.code_edge import CodeEdgeType
from app.models.code_node import CodeNodeType
from app.models.repository import IndexingStatus
from app.services.codebase.parser import parse_file
from app.services.codebase.types import (
    SKIP_DIRS,
    SKIP_FILE_PATTERNS,
    IndexingResult,
    ParsedFile,
    ParsedRelation,
)

logger = logging.getLogger(__name__)

# Batch size for bulk DB inserts (avoid enormous single transactions)
NODE_BATCH_SIZE = 2000
EDGE_BATCH_SIZE = 5000


class CodebaseIndexer:
    """Orchestrates codebase indexing: clone → parse → store.

    Usage:
        indexer = CodebaseIndexer(repo_id, github_token)
        result = await indexer.run(db)
    """

    def __init__(
        self,
        repo_id: uuid_pkg.UUID,
        github_token: str,
    ) -> None:
        self.repo_id = repo_id
        self.github_token = github_token
        self._clone_dir: str | None = None

    async def run(self, db: AsyncSession) -> IndexingResult:
        """Execute the full indexing pipeline."""
        repo = await repository_ops.get(db, self.repo_id)
        if not repo:
            raise ValueError(f"Repository {self.repo_id} not found")

        if not repo.full_name:
            raise ValueError(f"Repository {self.repo_id} has no full_name (required for clone)")

        # Mark indexing as started
        repo.indexing_status = IndexingStatus.INDEXING.value
        repo.index_error = None
        await db.flush()
        await db.commit()

        result = IndexingResult(
            repo_id=str(self.repo_id),
            files_parsed=0,
            nodes_created=0,
            edges_created=0,
            languages={},
        )

        try:
            # Step 1: Clone (do this BEFORE deleting old data — if clone fails,
            # the previous graph remains intact)
            clone_path = await self._clone_repo(repo.full_name)

            # Step 2: Delete existing graph data (re-index from scratch)
            nodes_deleted, edges_deleted = await code_graph_ops.delete_graph_for_repo(
                db, self.repo_id
            )
            if nodes_deleted or edges_deleted:
                logger.info(f"Cleared previous graph: {nodes_deleted} nodes, {edges_deleted} edges")

            # Step 3: Walk files and parse
            parsed_files = self._walk_and_parse(clone_path)
            result.files_parsed = len(parsed_files)

            # Step 4: Build and store graph
            nodes_created, edges_created, languages = await self._store_graph(db, parsed_files)
            result.nodes_created = nodes_created
            result.edges_created = edges_created
            result.languages = languages

            # Step 5: Mark complete
            repo = await repository_ops.get(db, self.repo_id)
            if repo:
                repo.indexing_status = IndexingStatus.COMPLETED.value
                repo.last_indexed_at = datetime.now(UTC)
                repo.index_error = None
                await db.flush()

            await db.commit()

            logger.info(
                f"Indexing complete for {repo.full_name if repo else self.repo_id}: "
                f"{result.files_parsed} files, {result.nodes_created} nodes, "
                f"{result.edges_created} edges"
            )

        except Exception as e:
            logger.error(f"Indexing failed for repo {self.repo_id}: {e}")
            result.errors.append(str(e))

            # Mark failed
            try:
                await db.rollback()
                repo = await repository_ops.get(db, self.repo_id)
                if repo:
                    repo.indexing_status = IndexingStatus.FAILED.value
                    repo.index_error = str(e)[:2000]
                    await db.flush()
                    await db.commit()
            except Exception:
                pass

        finally:
            # Cleanup clone directory
            self._cleanup()

        return result

    # ------------------------------------------------------------------
    # Step 1: Clone
    # ------------------------------------------------------------------

    async def _clone_repo(self, full_name: str) -> str:
        """Clone the repository to a temporary directory.

        Uses a shallow clone (depth=1) for speed — we only need the
        current file tree, not history.
        """
        self._clone_dir = tempfile.mkdtemp(prefix="trajan-index-")
        clone_url = f"https://x-access-token:{self.github_token}@github.com/{full_name}.git"

        proc = await asyncio.create_subprocess_exec(
            "git",
            "clone",
            "--depth=1",
            "--single-branch",
            clone_url,
            self._clone_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            error_msg = stderr.decode("utf-8", "replace").strip()
            raise RuntimeError(f"git clone failed: {error_msg}")

        logger.info(f"Cloned {full_name} to {self._clone_dir}")
        return self._clone_dir

    # ------------------------------------------------------------------
    # Step 2: Walk & Parse
    # ------------------------------------------------------------------

    def _walk_and_parse(self, clone_path: str) -> list[ParsedFile]:
        """Walk the file tree and parse each supported file."""
        root = Path(clone_path)
        parsed: list[ParsedFile] = []

        for path in sorted(root.rglob("*")):
            # Skip directories
            if path.is_dir():
                continue

            # Skip hidden/ignored directories
            rel_parts = path.relative_to(root).parts
            if any(part in SKIP_DIRS for part in rel_parts):
                continue

            # Skip known generated/binary patterns
            name = path.name
            if any(pat in name for pat in SKIP_FILE_PATTERNS):
                continue

            # Parse
            result = parse_file(str(path), clone_path)
            if result:
                parsed.append(result)

        logger.info(f"Parsed {len(parsed)} files from {clone_path}")
        return parsed

    # ------------------------------------------------------------------
    # Step 3: Store graph
    # ------------------------------------------------------------------

    async def _store_graph(
        self,
        db: AsyncSession,
        parsed_files: list[ParsedFile],
    ) -> tuple[int, int, dict[str, int]]:
        """Convert parsed files into CodeNode/CodeEdge records and store.

        Returns (nodes_created, edges_created, language_counts).
        """
        # Track language distribution
        languages: dict[str, int] = {}

        # Phase A: Create folder and file nodes, track symbol nodes
        # Maps: file_path → node UUID, (file_path, symbol_name) → node UUID
        file_node_ids: dict[str, uuid_pkg.UUID] = {}
        symbol_node_ids: dict[tuple[str, str], uuid_pkg.UUID] = {}
        folder_node_ids: dict[str, uuid_pkg.UUID] = {}
        all_node_dicts: list[dict[str, object]] = []
        all_edge_dicts: list[dict[str, object]] = []

        # Build folder hierarchy
        all_folders: set[str] = set()
        for pf in parsed_files:
            parts = Path(pf.file_path).parts
            for i in range(len(parts) - 1):
                folder = str(Path(*parts[: i + 1]))
                all_folders.add(folder)

        # Create folder nodes
        for folder in sorted(all_folders):
            node_id = uuid_pkg.uuid4()
            folder_node_ids[folder] = node_id
            all_node_dicts.append(
                {
                    "id": node_id,
                    "repo_id": self.repo_id,
                    "type": CodeNodeType.FOLDER,
                    "name": Path(folder).name,
                    "file_path": folder,
                }
            )

        # Create folder containment edges (parent → child)
        for folder in sorted(all_folders):
            parent = str(Path(folder).parent)
            if parent != "." and parent in folder_node_ids:
                all_edge_dicts.append(
                    {
                        "repo_id": self.repo_id,
                        "source_node_id": folder_node_ids[parent],
                        "target_node_id": folder_node_ids[folder],
                        "type": CodeEdgeType.CONTAINS,
                    }
                )

        # Create file nodes and symbol nodes
        for pf in parsed_files:
            languages[pf.language] = languages.get(pf.language, 0) + 1

            # File node
            file_id = uuid_pkg.uuid4()
            file_node_ids[pf.file_path] = file_id
            all_node_dicts.append(
                {
                    "id": file_id,
                    "repo_id": self.repo_id,
                    "type": CodeNodeType.FILE,
                    "name": Path(pf.file_path).name,
                    "file_path": pf.file_path,
                    "metadata_": {"language": pf.language},
                }
            )

            # Folder → File containment edge
            parent_dir = str(Path(pf.file_path).parent)
            if parent_dir != "." and parent_dir in folder_node_ids:
                all_edge_dicts.append(
                    {
                        "repo_id": self.repo_id,
                        "source_node_id": folder_node_ids[parent_dir],
                        "target_node_id": file_id,
                        "type": CodeEdgeType.CONTAINS,
                    }
                )

            # Symbol nodes
            for sym in pf.symbols:
                sym_id = uuid_pkg.uuid4()
                node_type = self._map_symbol_kind(sym.kind)
                symbol_node_ids[(pf.file_path, sym.name)] = sym_id
                all_node_dicts.append(
                    {
                        "id": sym_id,
                        "repo_id": self.repo_id,
                        "type": node_type,
                        "name": sym.name,
                        "file_path": pf.file_path,
                        "start_line": sym.start_line,
                        "end_line": sym.end_line,
                        "metadata_": sym.metadata,
                    }
                )

                # File → Symbol "defines" edge
                all_edge_dicts.append(
                    {
                        "repo_id": self.repo_id,
                        "source_node_id": file_id,
                        "target_node_id": sym_id,
                        "type": CodeEdgeType.DEFINES,
                    }
                )

        # Phase B: Create relationship edges from parsed relations
        all_relations: list[tuple[str, ParsedRelation]] = []
        for pf in parsed_files:
            for rel in pf.relations:
                all_relations.append((pf.file_path, rel))

        for _file_path, rel in all_relations:
            edge_type = self._map_relation_type(rel.relation_type)
            if not edge_type:
                continue

            # Resolve source node
            source_id = self._resolve_node_id(
                rel.source_name, rel.source_file, symbol_node_ids, file_node_ids
            )
            # Resolve target node (name-based lookup across all files)
            target_id = self._resolve_target(rel.target_name, symbol_node_ids, file_node_ids)

            if source_id and target_id and source_id != target_id:
                all_edge_dicts.append(
                    {
                        "repo_id": self.repo_id,
                        "source_node_id": source_id,
                        "target_node_id": target_id,
                        "type": edge_type,
                        "metadata_": rel.metadata,
                    }
                )

        # Phase C: Bulk insert in batches
        total_nodes = 0
        for i in range(0, len(all_node_dicts), NODE_BATCH_SIZE):
            batch = all_node_dicts[i : i + NODE_BATCH_SIZE]
            total_nodes += await code_graph_ops.bulk_insert_nodes(db, batch)

        total_edges = 0
        for i in range(0, len(all_edge_dicts), EDGE_BATCH_SIZE):
            batch = all_edge_dicts[i : i + EDGE_BATCH_SIZE]
            total_edges += await code_graph_ops.bulk_insert_edges(db, batch)

        return total_nodes, total_edges, languages

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _map_symbol_kind(kind: str) -> CodeNodeType:
        """Map a parsed symbol kind to a CodeNodeType enum."""
        mapping = {
            "class": CodeNodeType.CLASS,
            "function": CodeNodeType.FUNCTION,
            "method": CodeNodeType.METHOD,
            "interface": CodeNodeType.INTERFACE,
            "type": CodeNodeType.TYPE,
            "enum": CodeNodeType.ENUM,
            "import": CodeNodeType.IMPORT,
            "variable": CodeNodeType.VARIABLE,
        }
        return mapping.get(kind, CodeNodeType.VARIABLE)

    @staticmethod
    def _map_relation_type(rel_type: str) -> CodeEdgeType | None:
        """Map a parsed relation type to a CodeEdgeType enum."""
        mapping = {
            "imports": CodeEdgeType.IMPORTS,
            "calls": CodeEdgeType.CALLS,
            "extends": CodeEdgeType.EXTENDS,
            "implements": CodeEdgeType.IMPLEMENTS,
        }
        return mapping.get(rel_type)

    @staticmethod
    def _resolve_node_id(
        name: str,
        file_path: str,
        symbol_ids: dict[tuple[str, str], uuid_pkg.UUID],
        file_ids: dict[str, uuid_pkg.UUID],
    ) -> uuid_pkg.UUID | None:
        """Resolve a source reference to a node ID."""
        # Try exact symbol match in same file
        key = (file_path, name)
        if key in symbol_ids:
            return symbol_ids[key]
        # Fall back to file node
        return file_ids.get(file_path) or file_ids.get(name)

    @staticmethod
    def _resolve_target(
        name: str,
        symbol_ids: dict[tuple[str, str], uuid_pkg.UUID],
        file_ids: dict[str, uuid_pkg.UUID],
    ) -> uuid_pkg.UUID | None:
        """Resolve a target reference by name (cross-file lookup)."""
        # Strip common prefixes for call targets (e.g., "self.method" → "method")
        clean_name = name.split(".")[-1] if "." in name else name

        # Search all symbols for a name match
        for (_fp, sym_name), sym_id in symbol_ids.items():
            if sym_name in (clean_name, name):
                return sym_id

        # Try as file path
        return file_ids.get(name)

    def _cleanup(self) -> None:
        """Remove the temporary clone directory."""
        if self._clone_dir:
            shutil.rmtree(self._clone_dir, ignore_errors=True)
            self._clone_dir = None
