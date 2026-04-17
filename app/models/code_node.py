"""Code graph node model — represents a file, folder, class, function, etc.

Part of the Code Map codebase intelligence layer. Each node belongs to a
repository and represents a structural element extracted via tree-sitter AST
parsing. Visibility follows repository access (RLS).
"""

import enum
import uuid as uuid_pkg
from typing import Any

from sqlalchemy import Column, Enum, ForeignKey, Index, Integer, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlmodel import Field, SQLModel

from app.models.base import TimestampMixin, UUIDMixin


class CodeNodeType(str, enum.Enum):
    """Types of structural elements extracted from source code."""

    FOLDER = "folder"
    FILE = "file"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    INTERFACE = "interface"
    TYPE = "type"
    ENUM = "enum"
    IMPORT = "import"
    VARIABLE = "variable"


class CodeNode(UUIDMixin, TimestampMixin, SQLModel, table=True):
    """A node in the code knowledge graph.

    Represents a structural element (file, class, function, etc.) extracted
    from a repository's source code via tree-sitter AST parsing.
    """

    __tablename__ = "code_nodes"
    __table_args__ = (
        Index("ix_code_nodes_repo_type", "repo_id", "type"),
        Index("ix_code_nodes_repo_name", "repo_id", "name"),
        Index("ix_code_nodes_repo_path", "repo_id", "file_path"),
    )

    repo_id: uuid_pkg.UUID = Field(
        sa_column=Column(
            PG_UUID(as_uuid=True),
            ForeignKey("repositories.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
    )

    type: CodeNodeType = Field(
        sa_column=Column(
            Enum(
                CodeNodeType,
                name="codenodetype",
                native_enum=True,
                values_callable=lambda e: [m.value for m in e],
            ),
            nullable=False,
        ),
    )

    name: str = Field(sa_column=Column(Text, nullable=False))
    file_path: str = Field(sa_column=Column(Text, nullable=False))

    start_line: int | None = Field(
        default=None,
        sa_column=Column(Integer, nullable=True),
    )
    end_line: int | None = Field(
        default=None,
        sa_column=Column(Integer, nullable=True),
    )

    metadata_: dict[str, Any] | None = Field(
        default=None,
        sa_column=Column("metadata", JSONB, nullable=True),
    )
