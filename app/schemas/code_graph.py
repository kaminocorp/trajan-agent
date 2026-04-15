"""Pydantic schemas for Code Map API endpoints."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class CodeNodeRead(BaseModel):
    """A node in the code knowledge graph."""

    id: str
    repo_id: str
    type: str
    name: str
    file_path: str
    start_line: int | None = None
    end_line: int | None = None
    metadata: dict[str, Any] | None = None


class CodeEdgeRead(BaseModel):
    """An edge in the code knowledge graph."""

    id: str
    repo_id: str
    source_node_id: str
    target_node_id: str
    type: str
    metadata: dict[str, Any] | None = None


class CodeGraphResponse(BaseModel):
    """Full graph for a repository."""

    repo_id: str
    nodes: list[CodeNodeRead]
    edges: list[CodeEdgeRead]
    node_count: int
    edge_count: int


class IndexingStatusResponse(BaseModel):
    """Current indexing status for a repository."""

    repo_id: str
    indexing_status: str | None  # pending, indexing, completed, failed, or null
    last_indexed_at: datetime | None = None
    index_error: str | None = None
    node_count: int = 0
    edge_count: int = 0


class TriggerIndexingResponse(BaseModel):
    """Response for POST /repositories/{id}/index."""

    status: str  # "started", "already_running"
    message: str


class FileContentResponse(BaseModel):
    """Source code content for a file in the repository."""

    file_path: str
    content: str
    language: str | None = None
    size: int = 0


class FlowStep(BaseModel):
    """A single step in an execution flow."""

    node_id: str
    name: str
    type: str
    file_path: str
    start_line: int | None = None


class ExecutionFlow(BaseModel):
    """An auto-detected execution flow from an entry point."""

    entry_point: FlowStep
    steps: list[FlowStep]
    mermaid: str  # Pre-rendered Mermaid flowchart


class ExecutionFlowsResponse(BaseModel):
    """All detected execution flows for a repository."""

    repo_id: str
    flows: list[ExecutionFlow]
    entry_point_count: int
