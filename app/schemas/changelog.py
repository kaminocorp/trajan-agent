"""Pydantic schemas for changelog API endpoints."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel


class ChangelogCommitRead(BaseModel):
    """A commit linked to a changelog entry."""

    id: str
    commit_sha: str
    commit_message: str
    commit_author: str | None = None
    committed_at: str | None = None
    repository_id: str


class ChangelogEntryRead(BaseModel):
    """A changelog entry with its linked commits."""

    id: str
    product_id: str
    title: str
    summary: str
    category: str
    version: str | None = None
    entry_date: str
    is_ai_generated: bool
    is_published: bool
    created_at: datetime
    updated_at: datetime
    commits: list[ChangelogCommitRead] = []


class ChangelogEntryListResponse(BaseModel):
    """Paginated list of changelog entries."""

    entries: list[ChangelogEntryRead]
    total: int
    skip: int
    limit: int


class ChangelogEntryCreateRequest(BaseModel):
    """Request body for manually creating a changelog entry."""

    title: str
    summary: str
    category: str
    entry_date: str  # YYYY-MM-DD
    version: str | None = None
    is_published: bool = True
    commit_shas: list[str] | None = None


class ChangelogEntryUpdateRequest(BaseModel):
    """Request body for updating a changelog entry."""

    title: str | None = None
    summary: str | None = None
    category: str | None = None
    version: str | None = None
    is_published: bool | None = None


class GenerateChangelogResponse(BaseModel):
    """Response for POST /changelog/products/{id}/generate."""

    status: str  # "started", "already_running", "no_repos"
    message: str


class GenerationStatusResponse(BaseModel):
    """Response for GET /changelog/products/{id}/status."""

    status: str  # "idle", "generating", "complete", "error"
    progress: dict[str, Any] | None = None
