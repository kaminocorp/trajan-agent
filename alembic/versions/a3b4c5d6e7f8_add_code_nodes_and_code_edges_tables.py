"""Add code_nodes and code_edges tables for Code Map

Revision ID: a3b4c5d6e7f8
Revises: 7cba86c9058a
Create Date: 2026-04-15 12:00:00.000000

Creates the code_nodes and code_edges tables for the codebase intelligence
layer (Code Map). Also adds indexing_status, last_indexed_at, and index_error
columns to the repositories table.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a3b4c5d6e7f8"
down_revision: Union[str, None] = "7cba86c9058a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Enum definitions
codenodetype = postgresql.ENUM(
    "folder",
    "file",
    "class",
    "function",
    "method",
    "interface",
    "type",
    "enum",
    "import",
    "variable",
    name="codenodetype",
    create_type=False,
)

codeedgetype = postgresql.ENUM(
    "contains",
    "defines",
    "imports",
    "calls",
    "extends",
    "implements",
    name="codeedgetype",
    create_type=False,
)


def upgrade() -> None:
    # Create enum types
    codenodetype.create(op.get_bind(), checkfirst=True)
    codeedgetype.create(op.get_bind(), checkfirst=True)

    # --- code_nodes table ---
    op.create_table(
        "code_nodes",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("repo_id", sa.Uuid(), nullable=False),
        sa.Column("type", codenodetype, nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=True),
        sa.Column("end_line", sa.Integer(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["repo_id"],
            ["repositories.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_code_nodes_repo_id", "code_nodes", ["repo_id"])
    op.create_index("ix_code_nodes_repo_type", "code_nodes", ["repo_id", "type"])
    op.create_index("ix_code_nodes_repo_name", "code_nodes", ["repo_id", "name"])
    op.create_index("ix_code_nodes_repo_path", "code_nodes", ["repo_id", "file_path"])

    # --- code_edges table ---
    op.create_table(
        "code_edges",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("repo_id", sa.Uuid(), nullable=False),
        sa.Column("source_node_id", sa.Uuid(), nullable=False),
        sa.Column("target_node_id", sa.Uuid(), nullable=False),
        sa.Column("type", codeedgetype, nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["repo_id"],
            ["repositories.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_node_id"],
            ["code_nodes.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_node_id"],
            ["code_nodes.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_code_edges_repo_id", "code_edges", ["repo_id"])
    op.create_index("ix_code_edges_repo_source", "code_edges", ["repo_id", "source_node_id"])
    op.create_index("ix_code_edges_repo_target", "code_edges", ["repo_id", "target_node_id"])

    # --- Add indexing columns to repositories ---
    op.add_column(
        "repositories",
        sa.Column("indexing_status", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "repositories",
        sa.Column("last_indexed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "repositories",
        sa.Column("index_error", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    # Drop indexing columns from repositories
    op.drop_column("repositories", "index_error")
    op.drop_column("repositories", "last_indexed_at")
    op.drop_column("repositories", "indexing_status")

    # Drop code_edges table and indexes
    op.drop_index("ix_code_edges_repo_target", table_name="code_edges")
    op.drop_index("ix_code_edges_repo_source", table_name="code_edges")
    op.drop_index("ix_code_edges_repo_id", table_name="code_edges")
    op.drop_table("code_edges")

    # Drop code_nodes table and indexes
    op.drop_index("ix_code_nodes_repo_path", table_name="code_nodes")
    op.drop_index("ix_code_nodes_repo_name", table_name="code_nodes")
    op.drop_index("ix_code_nodes_repo_type", table_name="code_nodes")
    op.drop_index("ix_code_nodes_repo_id", table_name="code_nodes")
    op.drop_table("code_nodes")

    # Drop enum types
    codeedgetype.drop(op.get_bind(), checkfirst=True)
    codenodetype.drop(op.get_bind(), checkfirst=True)
