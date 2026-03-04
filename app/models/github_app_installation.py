"""GitHub App installation models.

Tracks which GitHub App installations are linked to which Trajan organizations,
and which specific repos each installation has access to (when using "selected" mode).
"""

import uuid as uuid_pkg
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlmodel import Field, SQLModel

from app.models.base import TimestampMixin, UUIDMixin


class GitHubAppInstallation(UUIDMixin, TimestampMixin, SQLModel, table=True):
    __tablename__ = "github_app_installations"

    installation_id: int = Field(unique=True, index=True)
    organization_id: uuid_pkg.UUID = Field(foreign_key="organizations.id", index=True)
    github_account_login: str = Field(max_length=255)
    github_account_type: str = Field(max_length=20)  # "Organization" | "User"
    installed_by_user_id: uuid_pkg.UUID = Field(foreign_key="users.id")
    permissions: dict = Field(default={}, sa_column=Column(JSONB, nullable=False, default={}))
    repository_selection: str = Field(max_length=20, default="all")
    suspended_at: datetime | None = Field(
        default=None,
        sa_type=DateTime(timezone=True),
    )


class GitHubAppInstallationRepo(UUIDMixin, TimestampMixin, SQLModel, table=True):
    __tablename__ = "github_app_installation_repos"

    installation_id: uuid_pkg.UUID = Field(
        sa_column=Column(
            PG_UUID(as_uuid=True),
            ForeignKey("github_app_installations.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
    )
    github_repo_id: int = Field(index=True)
    full_name: str = Field(max_length=500)
