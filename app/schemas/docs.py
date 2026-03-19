"""Pydantic schemas for documentation endpoints."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class GenerateDocsRequest(BaseModel):
    """Request body for POST /products/{id}/generate-docs."""

    mode: Literal["full", "additive"] = "full"
    # full: Regenerate all documentation from scratch
    # additive: Only add new docs, don't overwrite existing


class GenerateDocsResponse(BaseModel):
    """Response for POST /products/{id}/generate-docs."""

    status: str  # "started", "already_running"
    message: str


class DocsStatusResponse(BaseModel):
    """Response for GET /products/{id}/docs-status."""

    status: str  # "idle", "generating", "completed", "failed"
    progress: dict | None = None
    error: str | None = None
    last_generated_at: datetime | None = None


class DocumentGrouped(BaseModel):
    """A single document in the grouped response."""

    id: str
    title: str
    content: str | None
    type: str | None
    is_pinned: bool
    folder: dict | None
    created_at: str
    updated_at: str
    # Section-based organization (for Trajan Docs sectioned view)
    section: str | None = None
    subsection: str | None = None


class DocumentsGroupedResponse(BaseModel):
    """Response for GET /products/{id}/documents/grouped."""

    changelog: list[DocumentGrouped]
    blueprints: list[DocumentGrouped]
    plans: list[DocumentGrouped]
    executing: list[DocumentGrouped]
    completions: list[DocumentGrouped]
    archive: list[DocumentGrouped]


class ChangeEntryRequest(BaseModel):
    """A single changelog entry."""

    category: str  # "Added", "Changed", "Fixed", "Removed"
    description: str


class AddChangelogEntryRequest(BaseModel):
    """Request body for POST /products/{id}/changelog/add-entry."""

    version: str | None = None
    changes: list[ChangeEntryRequest]


# =============================================================================
# Phase 2: GitHub Sync Schemas
# =============================================================================


class ImportDocsResponse(BaseModel):
    """Response for POST /products/{id}/import-docs."""

    imported: int  # New documents created
    updated: int  # Existing documents updated
    skipped: int  # Unchanged documents skipped


class SyncDocsRequest(BaseModel):
    """Request body for POST /products/{id}/sync-docs."""

    document_ids: list[str] | None = None  # Specific docs to sync, or all with local changes
    message: str = "Sync documentation from Trajan"  # Commit message


class SyncDocsResponse(BaseModel):
    """Response for POST /products/{id}/sync-docs."""

    success: bool
    files_synced: int
    commit_sha: str | None = None
    branch: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None
    errors: list[str] = []


class DocumentSyncStatusResponse(BaseModel):
    """Sync status for a single document."""

    document_id: str
    status: str  # "synced", "local_changes", "remote_changes", "conflict", "error"
    local_sha: str | None = None
    remote_sha: str | None = None
    error: str | None = None


class DocsSyncStatusResponse(BaseModel):
    """Response for GET /products/{id}/docs-sync-status."""

    documents: list[DocumentSyncStatusResponse]
    has_local_changes: bool
    has_remote_changes: bool


class SyncConfigResponse(BaseModel):
    """Response for GET /repositories/{id}/sync-config."""

    sync_enabled: bool
    sync_branch: str | None = None
    sync_path_prefix: str = "docs/"
    sync_create_pr: bool = True
    sync_doc_filter: dict[str, Any] | None = None
    last_sync_commit_sha: str | None = None
    last_sync_pr_url: str | None = None


class SyncConfigUpdate(BaseModel):
    """Request body for PATCH /repositories/{id}/sync-config."""

    sync_enabled: bool | None = None
    sync_branch: str | None = Field(None, max_length=255)
    sync_path_prefix: str | None = Field(None, max_length=255)
    sync_create_pr: bool | None = None
    sync_doc_filter: dict[str, Any] | None = None


class PullRemoteRequest(BaseModel):
    """Request body for POST /documents/{id}/pull-remote."""

    pass  # No body needed, but keeping for future expansion


# =============================================================================
# Phase 7: Document Refresh Schemas
# =============================================================================


class RefreshDocumentResponse(BaseModel):
    """Response for POST /documents/{id}/refresh."""

    document_id: str
    status: str  # "updated", "unchanged", "error"
    changes_summary: str | None = None
    error: str | None = None


class RefreshDocumentDetailResponse(BaseModel):
    """Detail for a single document in bulk refresh."""

    document_id: str
    status: str  # "updated", "unchanged", "error"
    changes_summary: str | None = None
    error: str | None = None


class BulkRefreshResponse(BaseModel):
    """Response for POST /products/{id}/refresh-all-docs."""

    checked: int
    updated: int
    unchanged: int
    errors: int
    details: list[RefreshDocumentDetailResponse] = []


# =============================================================================
# Custom Documentation Request Schemas
# =============================================================================

# Type aliases for literal types
CustomDocType = Literal["how-to", "wiki", "overview", "technical", "guide"]
FormatStyle = Literal["technical", "presentation", "essay", "email", "how-to-guide"]
TargetAudience = Literal[
    "internal-technical", "internal-non-technical", "external-technical", "external-non-technical"
]


class CustomDocRequestSchema(BaseModel):
    """Request body for POST /products/{id}/documents/custom/generate."""

    prompt: str = Field(..., min_length=10, max_length=2000)
    doc_type: CustomDocType
    format_style: FormatStyle
    target_audience: TargetAudience
    focus_paths: list[str] | None = None
    title: str | None = Field(None, max_length=200)


class CustomDocResponseSchema(BaseModel):
    """Response for POST /products/{id}/documents/custom/generate."""

    job_id: str | None = None  # If background=True
    content: str | None = None  # If background=False (sync)
    suggested_title: str | None = None
    status: Literal["completed", "generating", "failed"]
    error: str | None = None
    generation_time_seconds: float | None = None


class CustomDocStatusSchema(BaseModel):
    """Response for GET /products/{id}/documents/custom/status/{job_id}."""

    status: Literal["generating", "completed", "failed", "cancelled"]
    progress: str | None = None  # e.g., "Analyzing codebase...", "Generating content..."
    content: str | None = None  # Available when completed
    suggested_title: str | None = None
    error: str | None = None


class SaveCustomDocSchema(BaseModel):
    """Request body for POST /documents/custom/{job_id}/save."""

    title: str = Field(..., min_length=1, max_length=200)
    folder: str = "blueprints"  # Default folder
    is_pinned: bool = False
