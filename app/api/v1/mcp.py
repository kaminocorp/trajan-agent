"""MCP API endpoints — API-key-authenticated data access for the MCP server.

These endpoints provide the same data as the authenticated endpoints but use
API key auth (Bearer trj_pk_xxx) instead of JWT, scoped to the key's product.
This allows the standalone trajan-mcp server to access product data.
"""

import logging
import uuid as uuid_pkg
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps.api_key_auth import require_scope
from app.core.database import get_db
from app.core.rate_limit import RateLimitConfig, rate_limiter
from app.domain.document_operations import document_ops
from app.domain.repository_operations import repository_ops
from app.domain.work_item_operations import work_item_ops
from app.models.document import Document
from app.models.product import Product
from app.models.product_api_key import ProductApiKey
from app.models.repository import Repository
from app.models.work_item import WorkItem
from app.services.github import GitHubService
from app.services.github.read_operations import GitHubReadOperations
from app.services.github.token_resolver import TokenResolver

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/mcp", tags=["MCP"])

# Rate limits for MCP endpoints (agents tend to be bursty)
MCP_READ_LIMIT = RateLimitConfig(requests=120, window_seconds=60)
MCP_WRITE_LIMIT = RateLimitConfig(requests=60, window_seconds=60)
MCP_ADMIN_LIMIT = RateLimitConfig(requests=10, window_seconds=60)

# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class MCPProductResponse(BaseModel):
    id: uuid_pkg.UUID
    name: str | None
    description: str | None
    analysis_status: str | None
    product_overview: dict[str, Any] | None
    doc_count: int
    work_item_count: int
    repository_count: int
    repositories: list[dict[str, Any]]


class MCPDocumentResponse(BaseModel):
    id: uuid_pkg.UUID
    title: str | None
    content: str | None
    type: str | None
    folder: dict[str, Any] | None
    section: str | None
    subsection: str | None
    is_generated: bool
    is_pinned: bool | None
    github_path: str | None
    sync_status: str | None
    created_at: datetime
    updated_at: datetime


class MCPDocumentListItem(BaseModel):
    id: uuid_pkg.UUID
    title: str | None
    type: str | None
    folder: dict[str, Any] | None
    section: str | None
    subsection: str | None
    is_generated: bool
    updated_at: datetime


class MCPDocumentListResponse(BaseModel):
    items: list[MCPDocumentListItem]
    total: int


class MCPWorkItemResponse(BaseModel):
    id: uuid_pkg.UUID
    title: str | None
    description: str | None
    type: str | None
    status: str | None
    priority: int | None
    tags: list[str] | None
    source: str | None
    reporter_email: str | None
    reporter_name: str | None
    created_at: datetime
    updated_at: datetime


class MCPWorkItemListItem(BaseModel):
    id: uuid_pkg.UUID
    title: str | None
    type: str | None
    status: str | None
    priority: int | None
    tags: list[str] | None
    updated_at: datetime


class MCPWorkItemListResponse(BaseModel):
    items: list[MCPWorkItemListItem]
    total: int


class MCPDocumentCreate(BaseModel):
    title: str
    content: str | None = None
    type: str | None = "note"
    folder: dict[str, Any] | None = None
    section: str | None = None
    subsection: str | None = None


class MCPWorkItemCreate(BaseModel):
    title: str
    description: str | None = None
    type: str | None = None
    priority: int | None = None
    tags: list[str] | None = None


class MCPDocumentUpdate(BaseModel):
    title: str | None = None
    content: str | None = None
    type: str | None = None
    folder: dict[str, Any] | None = None
    section: str | None = None
    subsection: str | None = None


class MCPWorkItemUpdate(BaseModel):
    title: str | None = None
    description: str | None = None
    type: str | None = None
    status: str | None = None
    priority: int | None = None
    tags: list[str] | None = None


class MCPSearchResult(BaseModel):
    id: uuid_pkg.UUID
    title: str | None
    snippet: str | None
    type: str | None
    folder: dict[str, Any] | None


class MCPSearchResponse(BaseModel):
    results: list[MCPSearchResult]
    total: int


class MCPRepositoryListItem(BaseModel):
    id: uuid_pkg.UUID
    name: str | None
    full_name: str | None
    language: str | None
    url: str | None
    default_branch: str | None


class MCPRepositoryListResponse(BaseModel):
    items: list[MCPRepositoryListItem]
    total: int


class MCPRepoTreeItem(BaseModel):
    path: str
    type: str  # "file" or "directory"
    size: int | None


class MCPRepoTreeResponse(BaseModel):
    repository_id: uuid_pkg.UUID
    repository_name: str | None
    branch: str | None
    files: list[MCPRepoTreeItem]
    truncated: bool


class MCPRepoFileResponse(BaseModel):
    repository_id: uuid_pkg.UUID
    path: str
    content: str
    size: int
    sha: str


# --- Phase 3: Admin schemas ---


class MCPGenerateDocsRequest(BaseModel):
    mode: Literal["full", "additive"] = "full"


class MCPGenerateDocsResponse(BaseModel):
    status: str  # "started", "already_running"
    message: str


class MCPDocsStatusResponse(BaseModel):
    status: str  # "idle", "generating", "completed", "failed"
    progress: dict[str, Any] | None = None
    error: str | None = None
    last_generated_at: datetime | None = None


class MCPCodebaseContextResponse(BaseModel):
    product_id: uuid_pkg.UUID
    name: str | None
    analysis_status: str | None
    product_overview: dict[str, Any] | None


class MCPSyncDocsRequest(BaseModel):
    document_ids: list[str] | None = None
    message: str = "Sync documentation from Trajan (via MCP)"


class MCPSyncDocsResponse(BaseModel):
    success: bool
    files_synced: int
    commit_sha: str | None = None
    branch: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None
    errors: list[str] = []


# ---------------------------------------------------------------------------
# Read endpoints (mcp:read scope)
# ---------------------------------------------------------------------------


@router.get("/product", response_model=MCPProductResponse)
async def get_product(
    api_key: ProductApiKey = Depends(require_scope("mcp:read")),
    db: AsyncSession = Depends(get_db),
) -> MCPProductResponse:
    """Get product overview for the API key's product."""
    rate_limiter.check_rate_limit(api_key.id, "mcp_read", MCP_READ_LIMIT)

    product = await db.get(Product, api_key.product_id)
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")

    # Count related entities
    doc_count = (
        await db.execute(
            select(func.count())
            .select_from(Document)
            .where(Document.product_id == api_key.product_id)  # type: ignore[arg-type]
        )
    ).scalar_one()

    work_item_count = (
        await db.execute(
            select(func.count())
            .select_from(WorkItem)
            .where(
                WorkItem.product_id == api_key.product_id,  # type: ignore[arg-type]
                WorkItem.deleted_at.is_(None),  # type: ignore[union-attr]
            )
        )
    ).scalar_one()

    repos_result = await db.execute(
        select(Repository).where(Repository.product_id == api_key.product_id)  # type: ignore[arg-type]
    )
    repos = repos_result.scalars().all()

    return MCPProductResponse(
        id=product.id,
        name=product.name,
        description=product.description,
        analysis_status=product.analysis_status,
        product_overview=product.product_overview,
        doc_count=doc_count,
        work_item_count=work_item_count,
        repository_count=len(repos),
        repositories=[
            {
                "id": str(r.id),
                "name": r.name,
                "full_name": r.full_name,
                "language": r.language,
                "url": r.url,
                "default_branch": r.default_branch,
            }
            for r in repos
        ],
    )


@router.get("/documents", response_model=MCPDocumentListResponse)
async def list_documents(
    api_key: ProductApiKey = Depends(require_scope("mcp:read")),
    db: AsyncSession = Depends(get_db),
    type: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> MCPDocumentListResponse:
    """List documents for the product (no content, to save tokens)."""
    rate_limiter.check_rate_limit(api_key.id, "mcp_read", MCP_READ_LIMIT)

    base_where = [Document.product_id == api_key.product_id]
    if type:
        base_where.append(Document.type == type)

    total = (
        await db.execute(select(func.count()).select_from(Document).where(*base_where))  # type: ignore[arg-type]
    ).scalar_one()

    result = await db.execute(
        select(Document)
        .where(*base_where)  # type: ignore[arg-type]
        .order_by(Document.updated_at.desc())  # type: ignore[attr-defined]
        .limit(limit)
        .offset(offset)
    )
    docs = result.scalars().all()

    return MCPDocumentListResponse(
        items=[
            MCPDocumentListItem(
                id=d.id,
                title=d.title,
                type=d.type,
                folder=d.folder,
                section=d.section,
                subsection=d.subsection,
                is_generated=d.is_generated,
                updated_at=d.updated_at,
            )
            for d in docs
        ],
        total=total,
    )


@router.get("/documents/search", response_model=MCPSearchResponse)
async def search_documents(
    q: str = Query(..., min_length=1, max_length=500),
    api_key: ProductApiKey = Depends(require_scope("mcp:read")),
    db: AsyncSession = Depends(get_db),
    type: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
) -> MCPSearchResponse:
    """Full-text search across document titles and content."""
    rate_limiter.check_rate_limit(api_key.id, "mcp_read", MCP_READ_LIMIT)

    escaped_q = q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")

    base_where = [
        Document.product_id == api_key.product_id,
        (
            Document.title.ilike(f"%{escaped_q}%", escape="\\")  # type: ignore[union-attr]
            | Document.content.ilike(f"%{escaped_q}%", escape="\\")  # type: ignore[union-attr]
        ),
    ]
    if type:
        base_where.append(Document.type == type)

    total = (
        await db.execute(select(func.count()).select_from(Document).where(*base_where))
    ).scalar_one()

    result = await db.execute(select(Document).where(*base_where).limit(limit))
    docs = result.scalars().all()

    results = []
    for doc in docs:
        # Extract a snippet around the match
        snippet = None
        if doc.content:
            lower_content = doc.content.lower()
            idx = lower_content.find(q.lower())
            if idx >= 0:
                start = max(0, idx - 80)
                end = min(len(doc.content), idx + len(q) + 80)
                snippet = (
                    ("..." if start > 0 else "")
                    + doc.content[start:end]
                    + ("..." if end < len(doc.content) else "")
                )
            else:
                snippet = doc.content[:200] + ("..." if len(doc.content) > 200 else "")

        results.append(
            MCPSearchResult(
                id=doc.id,
                title=doc.title,
                snippet=snippet,
                type=doc.type,
                folder=doc.folder,
            )
        )

    return MCPSearchResponse(results=results, total=total)


@router.get("/documents/{document_id}", response_model=MCPDocumentResponse)
async def get_document(
    document_id: uuid_pkg.UUID,
    api_key: ProductApiKey = Depends(require_scope("mcp:read")),
    db: AsyncSession = Depends(get_db),
) -> MCPDocumentResponse:
    """Get a single document with full content."""
    rate_limiter.check_rate_limit(api_key.id, "mcp_read", MCP_READ_LIMIT)

    result = await db.execute(
        select(Document).where(
            Document.id == document_id,  # type: ignore[arg-type]
            Document.product_id == api_key.product_id,  # type: ignore[arg-type]
        )
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    return MCPDocumentResponse(
        id=doc.id,
        title=doc.title,
        content=doc.content,
        type=doc.type,
        folder=doc.folder,
        section=doc.section,
        subsection=doc.subsection,
        is_generated=doc.is_generated,
        is_pinned=doc.is_pinned,
        github_path=doc.github_path,
        sync_status=doc.sync_status,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
    )


@router.get("/work-items", response_model=MCPWorkItemListResponse)
async def list_work_items(
    api_key: ProductApiKey = Depends(require_scope("mcp:read")),
    db: AsyncSession = Depends(get_db),
    status_filter: str | None = Query(None, alias="status"),
    type: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> MCPWorkItemListResponse:
    """List work items for the product."""
    rate_limiter.check_rate_limit(api_key.id, "mcp_read", MCP_READ_LIMIT)

    base_where = [
        WorkItem.product_id == api_key.product_id,
        WorkItem.deleted_at.is_(None),  # type: ignore[union-attr]
    ]
    if status_filter:
        base_where.append(WorkItem.status == status_filter)
    if type:
        base_where.append(WorkItem.type == type)

    total = (
        await db.execute(select(func.count()).select_from(WorkItem).where(*base_where))
    ).scalar_one()

    result = await db.execute(
        select(WorkItem)
        .where(*base_where)
        .order_by(WorkItem.updated_at.desc())  # type: ignore[attr-defined]
        .limit(limit)
        .offset(offset)
    )
    items = result.scalars().all()

    return MCPWorkItemListResponse(
        items=[
            MCPWorkItemListItem(
                id=i.id,
                title=i.title,
                type=i.type,
                status=i.status,
                priority=i.priority,
                tags=i.tags,
                updated_at=i.updated_at,
            )
            for i in items
        ],
        total=total,
    )


@router.get("/work-items/{work_item_id}", response_model=MCPWorkItemResponse)
async def get_work_item(
    work_item_id: uuid_pkg.UUID,
    api_key: ProductApiKey = Depends(require_scope("mcp:read")),
    db: AsyncSession = Depends(get_db),
) -> MCPWorkItemResponse:
    """Get a single work item with full detail."""
    rate_limiter.check_rate_limit(api_key.id, "mcp_read", MCP_READ_LIMIT)

    result = await db.execute(
        select(WorkItem).where(
            WorkItem.id == work_item_id,  # type: ignore[arg-type]
            WorkItem.product_id == api_key.product_id,  # type: ignore[arg-type]
            WorkItem.deleted_at.is_(None),  # type: ignore[union-attr]
        )
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Work item not found")

    return MCPWorkItemResponse(
        id=item.id,
        title=item.title,
        description=item.description,
        type=item.type,
        status=item.status,
        priority=item.priority,
        tags=item.tags,
        source=item.source,
        reporter_email=item.reporter_email,
        reporter_name=item.reporter_name,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


# ---------------------------------------------------------------------------
# Repository endpoints (mcp:read scope)
# ---------------------------------------------------------------------------


@router.get("/repositories", response_model=MCPRepositoryListResponse)
async def list_repositories(
    api_key: ProductApiKey = Depends(require_scope("mcp:read")),
    db: AsyncSession = Depends(get_db),
) -> MCPRepositoryListResponse:
    """List repositories linked to the product."""
    rate_limiter.check_rate_limit(api_key.id, "mcp_read", MCP_READ_LIMIT)

    result = await db.execute(
        select(Repository).where(
            Repository.product_id == api_key.product_id  # type: ignore[arg-type]
        )
    )
    repos = result.scalars().all()

    return MCPRepositoryListResponse(
        items=[
            MCPRepositoryListItem(
                id=r.id,
                name=r.name,
                full_name=r.full_name,
                language=r.language,
                url=r.url,
                default_branch=r.default_branch,
            )
            for r in repos
        ],
        total=len(repos),
    )


@router.get("/repositories/{repository_id}/tree", response_model=MCPRepoTreeResponse)
async def get_repository_tree(
    repository_id: uuid_pkg.UUID,
    api_key: ProductApiKey = Depends(require_scope("mcp:read")),
    db: AsyncSession = Depends(get_db),
    branch: str | None = Query(None, description="Branch name (defaults to repo default branch)"),
) -> MCPRepoTreeResponse:
    """Get the file tree of a linked repository via GitHub API."""
    rate_limiter.check_rate_limit(api_key.id, "mcp_read", MCP_READ_LIMIT)

    result = await db.execute(
        select(Repository).where(
            Repository.id == repository_id,  # type: ignore[arg-type]
            Repository.product_id == api_key.product_id,  # type: ignore[arg-type]
        )
    )
    repo = result.scalar_one_or_none()
    if not repo:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repository not found")

    if not repo.full_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Repository has no linked GitHub full_name",
        )

    resolver = TokenResolver(db)
    token, method = await resolver.resolve_token(repo, api_key.created_by_user_id)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No GitHub token available for this repository",
        )

    owner, repo_name = repo.full_name.split("/", 1)
    target_branch = branch or repo.default_branch or "main"

    try:
        gh = GitHubReadOperations(token=token)
        tree = await gh.get_repo_tree(owner=owner, repo=repo_name, branch=target_branch)
    except Exception as e:
        logger.error(f"GitHub tree fetch failed for {repo.full_name}: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to fetch repository tree from GitHub: {e}",
        ) from None

    items = [
        MCPRepoTreeItem(
            path=item.path, type="file" if item.type == "blob" else "directory", size=item.size
        )
        for item in tree.all_items
    ]

    return MCPRepoTreeResponse(
        repository_id=repo.id,
        repository_name=repo.name,
        branch=target_branch,
        files=items,
        truncated=tree.truncated,
    )


@router.get("/repositories/{repository_id}/file", response_model=MCPRepoFileResponse)
async def get_repository_file(
    repository_id: uuid_pkg.UUID,
    path: str = Query(..., min_length=1, max_length=500, description="File path in the repository"),
    api_key: ProductApiKey = Depends(require_scope("mcp:read")),
    db: AsyncSession = Depends(get_db),
    branch: str | None = Query(None, description="Branch name (defaults to repo default branch)"),
) -> MCPRepoFileResponse:
    """Get the content of a single file from a linked repository via GitHub API."""
    rate_limiter.check_rate_limit(api_key.id, "mcp_read", MCP_READ_LIMIT)

    result = await db.execute(
        select(Repository).where(
            Repository.id == repository_id,  # type: ignore[arg-type]
            Repository.product_id == api_key.product_id,  # type: ignore[arg-type]
        )
    )
    repo = result.scalar_one_or_none()
    if not repo:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Repository not found")

    if not repo.full_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Repository has no linked GitHub full_name",
        )

    resolver = TokenResolver(db)
    token, method = await resolver.resolve_token(repo, api_key.created_by_user_id)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No GitHub token available for this repository",
        )

    owner, repo_name = repo.full_name.split("/", 1)
    target_branch = branch or repo.default_branch or "main"

    try:
        gh = GitHubReadOperations(token=token)
        file = await gh.get_file_content(
            owner=owner, repo=repo_name, path=path, branch=target_branch
        )
    except Exception as e:
        logger.error(f"GitHub file fetch failed for {repo.full_name}:{path}: {e}")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to fetch file from GitHub: {e}",
        ) from None

    if file is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File not found, is binary, or exceeds size limit",
        )

    return MCPRepoFileResponse(
        repository_id=repo.id,
        path=file.path,
        content=file.content,
        size=file.size,
        sha=file.sha,
    )


# ---------------------------------------------------------------------------
# Write endpoints (mcp:write scope)
# ---------------------------------------------------------------------------


@router.post(
    "/documents",
    response_model=MCPDocumentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_document(
    data: MCPDocumentCreate,
    api_key: ProductApiKey = Depends(require_scope("mcp:write")),
    db: AsyncSession = Depends(get_db),
) -> MCPDocumentResponse:
    """Create a new document in the product."""
    rate_limiter.check_rate_limit(api_key.id, "mcp_write", MCP_WRITE_LIMIT)

    doc = await document_ops.create(
        db,
        obj_in={
            "product_id": api_key.product_id,
            "title": data.title,
            "content": data.content,
            "type": data.type,
            "folder": data.folder,
            "section": data.section,
            "subsection": data.subsection,
            "is_generated": False,
        },
        created_by_user_id=api_key.created_by_user_id,
    )

    return MCPDocumentResponse(
        id=doc.id,
        title=doc.title,
        content=doc.content,
        type=doc.type,
        folder=doc.folder,
        section=doc.section,
        subsection=doc.subsection,
        is_generated=doc.is_generated,
        is_pinned=doc.is_pinned,
        github_path=doc.github_path,
        sync_status=doc.sync_status,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
    )


@router.patch("/documents/{document_id}", response_model=MCPDocumentResponse)
async def update_document(
    document_id: uuid_pkg.UUID,
    data: MCPDocumentUpdate,
    api_key: ProductApiKey = Depends(require_scope("mcp:write")),
    db: AsyncSession = Depends(get_db),
) -> MCPDocumentResponse:
    """Update an existing document."""
    rate_limiter.check_rate_limit(api_key.id, "mcp_write", MCP_WRITE_LIMIT)

    result = await db.execute(
        select(Document).where(
            Document.id == document_id,  # type: ignore[arg-type]
            Document.product_id == api_key.product_id,  # type: ignore[arg-type]
        )
    )
    doc = result.scalar_one_or_none()
    if not doc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")

    update_data = data.model_dump(exclude_unset=True)
    doc = await document_ops.update(db, db_obj=doc, obj_in=update_data)

    return MCPDocumentResponse(
        id=doc.id,
        title=doc.title,
        content=doc.content,
        type=doc.type,
        folder=doc.folder,
        section=doc.section,
        subsection=doc.subsection,
        is_generated=doc.is_generated,
        is_pinned=doc.is_pinned,
        github_path=doc.github_path,
        sync_status=doc.sync_status,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
    )


@router.post(
    "/work-items",
    response_model=MCPWorkItemResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_work_item(
    data: MCPWorkItemCreate,
    api_key: ProductApiKey = Depends(require_scope("mcp:write")),
    db: AsyncSession = Depends(get_db),
) -> MCPWorkItemResponse:
    """Create a new work item in the product."""
    rate_limiter.check_rate_limit(api_key.id, "mcp_write", MCP_WRITE_LIMIT)

    item = await work_item_ops.create(
        db,
        obj_in={
            "product_id": api_key.product_id,
            "title": data.title,
            "description": data.description,
            "type": data.type,
            "status": "reported",
            "priority": data.priority,
            "tags": data.tags,
            "source": "mcp",
        },
        created_by_user_id=api_key.created_by_user_id,
    )

    return MCPWorkItemResponse(
        id=item.id,
        title=item.title,
        description=item.description,
        type=item.type,
        status=item.status,
        priority=item.priority,
        tags=item.tags,
        source=item.source,
        reporter_email=item.reporter_email,
        reporter_name=item.reporter_name,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


@router.patch("/work-items/{work_item_id}", response_model=MCPWorkItemResponse)
async def update_work_item(
    work_item_id: uuid_pkg.UUID,
    data: MCPWorkItemUpdate,
    api_key: ProductApiKey = Depends(require_scope("mcp:write")),
    db: AsyncSession = Depends(get_db),
) -> MCPWorkItemResponse:
    """Update an existing work item."""
    rate_limiter.check_rate_limit(api_key.id, "mcp_write", MCP_WRITE_LIMIT)

    result = await db.execute(
        select(WorkItem).where(
            WorkItem.id == work_item_id,  # type: ignore[arg-type]
            WorkItem.product_id == api_key.product_id,  # type: ignore[arg-type]
            WorkItem.deleted_at.is_(None),  # type: ignore[union-attr]
        )
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Work item not found")

    update_data = data.model_dump(exclude_unset=True)
    item = await work_item_ops.update(db, db_obj=item, obj_in=update_data)

    return MCPWorkItemResponse(
        id=item.id,
        title=item.title,
        description=item.description,
        type=item.type,
        status=item.status,
        priority=item.priority,
        tags=item.tags,
        source=item.source,
        reporter_email=item.reporter_email,
        reporter_name=item.reporter_name,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


# ---------------------------------------------------------------------------
# Admin endpoints (mcp:admin scope)
# ---------------------------------------------------------------------------


@router.post("/generate-docs", response_model=MCPGenerateDocsResponse)
async def generate_docs(
    background_tasks: BackgroundTasks,
    data: MCPGenerateDocsRequest | None = None,
    api_key: ProductApiKey = Depends(require_scope("mcp:admin")),
    db: AsyncSession = Depends(get_db),
) -> MCPGenerateDocsResponse:
    """Trigger AI documentation generation for the product.

    Runs as a background task. Poll GET /api/v1/mcp/docs-status for progress.
    """
    rate_limiter.check_rate_limit(api_key.id, "mcp_admin", MCP_ADMIN_LIMIT)

    product = await db.get(Product, api_key.product_id)
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")

    if product.docs_generation_status == "generating":
        return MCPGenerateDocsResponse(
            status="already_running",
            message="Documentation generation already in progress",
        )

    mode = data.mode if data else "full"

    product.docs_generation_status = "generating"
    product.docs_generation_error = None
    product.docs_generation_progress = None
    db.add(product)
    await db.commit()

    # Import here to avoid circular imports — same pattern as docs_generation.py
    from app.api.v1.products.docs_generation import run_document_orchestrator

    background_tasks.add_task(
        run_document_orchestrator,
        product_id=str(api_key.product_id),
        user_id=str(api_key.created_by_user_id),
        mode=mode,
    )

    return MCPGenerateDocsResponse(
        status="started",
        message=f"Documentation generation started (mode: {mode}). "
        "Poll GET /api/v1/mcp/docs-status for progress.",
    )


@router.get("/docs-status", response_model=MCPDocsStatusResponse)
async def get_docs_status(
    api_key: ProductApiKey = Depends(require_scope("mcp:read")),
    db: AsyncSession = Depends(get_db),
) -> MCPDocsStatusResponse:
    """Get current documentation generation status (for polling after generate-docs)."""
    rate_limiter.check_rate_limit(api_key.id, "mcp_read", MCP_READ_LIMIT)

    product = await db.get(Product, api_key.product_id)
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")

    return MCPDocsStatusResponse(
        status=product.docs_generation_status or "idle",
        progress=product.docs_generation_progress,
        error=product.docs_generation_error,
        last_generated_at=product.last_docs_generated_at,
    )


@router.get("/codebase-context", response_model=MCPCodebaseContextResponse)
async def get_codebase_context(
    api_key: ProductApiKey = Depends(require_scope("mcp:read")),
    db: AsyncSession = Depends(get_db),
) -> MCPCodebaseContextResponse:
    """Get the stored codebase analysis context for the product.

    Returns the product overview generated by AI analysis — includes tech stack,
    architecture, API endpoints, database models, services, and frontend pages.
    Run product analysis from the Trajan UI first if this returns null.
    """
    rate_limiter.check_rate_limit(api_key.id, "mcp_read", MCP_READ_LIMIT)

    product = await db.get(Product, api_key.product_id)
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")

    return MCPCodebaseContextResponse(
        product_id=product.id,
        name=product.name,
        analysis_status=product.analysis_status,
        product_overview=product.product_overview,
    )


@router.post("/sync-docs", response_model=MCPSyncDocsResponse)
async def sync_docs_to_repo(
    data: MCPSyncDocsRequest | None = None,
    api_key: ProductApiKey = Depends(require_scope("mcp:admin")),
    db: AsyncSession = Depends(get_db),
) -> MCPSyncDocsResponse:
    """Push documents to the linked GitHub repository.

    Uses the repository's sync configuration (branch, path prefix, PR mode).
    Sync must be enabled on the repository first.
    """
    rate_limiter.check_rate_limit(api_key.id, "mcp_admin", MCP_ADMIN_LIMIT)

    request = data or MCPSyncDocsRequest()

    # Get documents to sync
    if request.document_ids:
        doc_uuids = [uuid_pkg.UUID(d) for d in request.document_ids]
        result = await db.execute(
            select(Document).where(
                Document.id.in_(doc_uuids),  # type: ignore[union-attr]
                Document.product_id == api_key.product_id,  # type: ignore[arg-type]
            )
        )
        documents = list(result.scalars().all())
    else:
        documents = await document_ops.get_with_local_changes(db, product_id=api_key.product_id)

    if not documents:
        return MCPSyncDocsResponse(
            success=True,
            files_synced=0,
            errors=["No documents to sync"],
        )

    # Get primary GitHub repository
    repos = await repository_ops.get_github_repos_by_product(db, product_id=api_key.product_id)
    if not repos:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No GitHub repositories linked to this product",
        )

    repo = repos[0]

    if not repo.sync_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Sync is not enabled for this repository. "
            "Enable it in Product Settings > Sync before pushing.",
        )

    # Resolve GitHub token
    resolver = TokenResolver(db)
    github_token, token_method = await resolver.resolve_token(repo, api_key.created_by_user_id)
    if not github_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No GitHub token available for this repository",
        )

    # Check write permission for GitHub App tokens
    if token_method == "github_app":
        product = await db.get(Product, api_key.product_id)
        if product and product.organization_id:
            from app.domain import github_app_installation_ops

            installation = await github_app_installation_ops.get_for_org(
                db, product.organization_id
            )
            if installation and installation.permissions.get("contents") == "read":
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="GitHub App has read-only 'contents' permission. "
                    "Sync requires 'contents: write'.",
                )

    from app.services.docs.sync_service import DocsSyncService

    github_service = GitHubService(github_token)
    sync_service = DocsSyncService(db, github_service)
    sync_result = await sync_service.sync_to_repo(documents, repo, request.message)

    return MCPSyncDocsResponse(
        success=sync_result.success,
        files_synced=sync_result.files_synced,
        commit_sha=sync_result.commit_sha,
        branch=sync_result.branch,
        pr_url=sync_result.pr_url,
        pr_number=sync_result.pr_number,
        errors=sync_result.errors,
    )
