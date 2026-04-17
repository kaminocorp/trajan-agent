"""Code graph edge model — represents a relationship between two code nodes.

Part of the Code Map codebase intelligence layer. Each edge connects two
CodeNode records within the same repository: imports, calls, inheritance, etc.
"""

import enum
import uuid as uuid_pkg
from typing import Any

from sqlalchemy import Column, Enum, ForeignKey, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlmodel import Field, SQLModel

from app.models.base import TimestampMixin, UUIDMixin


class CodeEdgeType(str, enum.Enum):
    """Types of relationships between code nodes."""

    CONTAINS = "contains"
    DEFINES = "defines"
    IMPORTS = "imports"
    CALLS = "calls"
    EXTENDS = "extends"
    IMPLEMENTS = "implements"


class CodeEdge(UUIDMixin, TimestampMixin, SQLModel, table=True):
    """An edge in the code knowledge graph.

    Represents a relationship (import, call, inheritance, etc.) between
    two CodeNode records within the same repository.
    """

    __tablename__ = "code_edges"
    __table_args__ = (
        Index("ix_code_edges_repo_source", "repo_id", "source_node_id"),
        Index("ix_code_edges_repo_target", "repo_id", "target_node_id"),
    )

    repo_id: uuid_pkg.UUID = Field(
        sa_column=Column(
            PG_UUID(as_uuid=True),
            ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
    )

    source_node_id: uuid_pkg.UUID = Field(
        sa_column=Column(
            PG_UUID(as_uuid=True),
            ForeignKey("code_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )

    target_node_id: uuid_pkg.UUID = Field(
        sa_column=Column(
            PG_UUID(as_uuid=True),
            ForeignKey("code_nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )

    type: CodeEdgeType = Field(
        sa_column=Column(
            Enum(
                CodeEdgeType,
                name="codeedgetype",
                native_enum=True,
                values_callable=lambda e: [m.value for m in e],
            ),
            nullable=False,
        ),
    )

    metadata_: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column("metadata", JSONB, nullable=True),
    )
