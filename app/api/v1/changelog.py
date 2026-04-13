"""Changelog API: AI-generated, commit-grouped project history."""

import logging
import uuid as uuid_pkg
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    check_product_editor_access,
    check_product_viewer_access,
    get_current_user,
    get_db_with_rls,
)
from app.api.v1.progress.utils import resolve_github_token
from app.domain import changelog_ops, product_ops, repository_ops
from app.models.user import User
from app.schemas.changelog import (
    ChangelogCommitRead,
    ChangelogEntryCreateRequest,
    ChangelogEntryListResponse,
    ChangelogEntryRead,
    ChangelogEntryUpdateRequest,
    GenerateChangelogResponse,
    GenerationStatusResponse,
)

router = APIRouter(prefix="/changelog", tags=["changelog"])
logger = logging.getLogger(__name__)

VALID_CATEGORIES = {"added", "changed", "fixed", "removed", "security", "infrastructure", "other"}


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _serialize_entry(entry: object) -> ChangelogEntryRead:
    """Convert a ChangelogEntry ORM object to a response schema."""
    commits = []
    for c in getattr(entry, "commits", []):
        commits.append(
            ChangelogCommitRead(
                id=str(c.id),
                commit_sha=c.commit_sha,
                commit_message=c.commit_message,
                commit_author=c.commit_author,
                committed_at=c.committed_at,
                repository_id=str(c.repository_id),
            )
        )

    return ChangelogEntryRead(
        id=str(entry.id),
        product_id=str(entry.product_id),
        title=entry.title,
        summary=entry.summary,
        category=entry.category,
        version=entry.version,
        entry_date=entry.entry_date,
        is_ai_generated=entry.is_ai_generated,
        is_published=entry.is_published,
        created_at=entry.created_at,
        updated_at=entry.updated_at,
        commits=commits,
    )


# ---------------------------------------------------------------------------
# List & Read
# ---------------------------------------------------------------------------


@router.get(
    "/products/{product_id}",
    response_model=ChangelogEntryListResponse,
)
async def list_changelog_entries(
    product_id: uuid_pkg.UUID,
    category: str | None = Query(None, description="Filter by category"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> ChangelogEntryListResponse:
    """List changelog entries for a product (paginated, newest first).

    Requires viewer access.
    """
    await check_product_viewer_access(db, product_id, current_user.id)

    entries = await changelog_ops.get_entries_by_product(
        db, product_id, category=category, skip=skip, limit=limit
    )
    total = await changelog_ops.count_by_product(db, product_id)

    return ChangelogEntryListResponse(
        entries=[_serialize_entry(e) for e in entries],
        total=total,
        skip=skip,
        limit=limit,
    )


@router.get(
    "/entries/{entry_id}",
    response_model=ChangelogEntryRead,
)
async def get_changelog_entry(
    entry_id: uuid_pkg.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> ChangelogEntryRead:
    """Get a single changelog entry with its commits.

    Requires viewer access to the entry's product.
    """
    entry = await changelog_ops.get(db, entry_id)
    if not entry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Changelog entry not found",
        )

    await check_product_viewer_access(db, entry.product_id, current_user.id)
    return _serialize_entry(entry)


# ---------------------------------------------------------------------------
# Generate (AI)
# ---------------------------------------------------------------------------


@router.post(
    "/products/{product_id}/generate",
    response_model=GenerateChangelogResponse,
)
async def generate_changelog(
    product_id: uuid_pkg.UUID,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> GenerateChangelogResponse:
    """Trigger AI changelog generation for unprocessed commits.

    Fetches full commit history, diffs against already-processed SHAs,
    and groups new commits into changelog entries using Claude.
    Processes in batches of ~75 commits, oldest-first.

    Requires editor access.
    """
    await check_product_editor_access(db, product_id, current_user.id)

    product = await product_ops.get(db, product_id)
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found",
        )

    # Check for already-running generation
    progress = product.docs_generation_progress
    if isinstance(progress, dict) and progress.get("type") == "changelog":
        stage = progress.get("stage", "")
        if stage in ("fetching", "processing"):
            return GenerateChangelogResponse(
                status="already_running",
                message="Changelog generation already in progress.",
            )

    # Check repos
    repos = await repository_ops.get_github_repos_by_product(db, product_id=product_id)
    if not repos:
        return GenerateChangelogResponse(
            status="no_repos",
            message="No GitHub repositories linked. Add a repository first.",
        )

    # Check GitHub access
    github_token = await resolve_github_token(db, current_user, product_id)
    if not github_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No GitHub access configured. Install the GitHub App, "
            "add a Personal Access Token, or link repos with a fine-grained token.",
        )

    # Write initial "starting" progress before queuing the background task so the
    # frontend status poll never sees "idle" in the gap between this response and
    # the background task's first _emit_progress("fetching") write.
    product.docs_generation_progress = {
        "type": "changelog",
        "stage": "starting",
        "message": "Changelog generation queued...",
        "batch_current": 0,
        "batch_total": 0,
        "entries_created": 0,
        "commits_processed": 0,
        "updated_at": datetime.now(UTC).isoformat(),
    }
    await db.commit()

    # Start background task
    background_tasks.add_task(
        _run_changelog_generation,
        product_id=str(product_id),
        user_id=str(current_user.id),
        github_token=github_token,
    )

    return GenerateChangelogResponse(
        status="started",
        message="Changelog generation started. Poll for progress.",
    )


@router.get(
    "/products/{product_id}/status",
    response_model=GenerationStatusResponse,
)
async def get_changelog_status(
    product_id: uuid_pkg.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> GenerationStatusResponse:
    """Get current changelog generation status.

    Requires viewer access.
    """
    await check_product_viewer_access(db, product_id, current_user.id)

    product = await product_ops.get(db, product_id)
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found",
        )

    progress = product.docs_generation_progress
    if isinstance(progress, dict) and progress.get("type") == "changelog":
        return GenerationStatusResponse(
            status=progress.get("stage", "idle"),
            progress=progress,
        )

    return GenerationStatusResponse(status="idle", progress=None)


# ---------------------------------------------------------------------------
# Manual create / edit / delete
# ---------------------------------------------------------------------------


@router.post(
    "/entries",
    response_model=ChangelogEntryRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_changelog_entry(
    request: ChangelogEntryCreateRequest,
    product_id: uuid_pkg.UUID = Query(..., description="Product ID"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> ChangelogEntryRead:
    """Manually create a changelog entry (with optional commit SHAs).

    Requires editor access.
    """
    await check_product_editor_access(db, product_id, current_user.id)

    category = request.category.lower().strip()
    if category not in VALID_CATEGORIES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid category. Must be one of: {', '.join(sorted(VALID_CATEGORIES))}",
        )

    entry = await changelog_ops.create_entry_with_commits(
        db=db,
        entry_data={
            "product_id": product_id,
            "title": request.title,
            "summary": request.summary,
            "category": category,
            "entry_date": request.entry_date,
            "version": request.version,
            "is_ai_generated": False,
            "is_published": request.is_published,
        },
        user_id=current_user.id,
    )
    await db.commit()

    # Re-fetch with commits loaded
    refreshed = await changelog_ops.get(db, entry.id)
    if refreshed is None:
        raise HTTPException(status_code=404, detail="Changelog entry not found after creation")
    return _serialize_entry(refreshed)


@router.patch(
    "/entries/{entry_id}",
    response_model=ChangelogEntryRead,
)
async def update_changelog_entry(
    entry_id: uuid_pkg.UUID,
    request: ChangelogEntryUpdateRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> ChangelogEntryRead:
    """Update a changelog entry's title, summary, category, etc.

    Requires editor access.
    """
    entry = await changelog_ops.get(db, entry_id)
    if not entry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Changelog entry not found",
        )

    await check_product_editor_access(db, entry.product_id, current_user.id)

    update_data = request.model_dump(exclude_unset=True)

    # Validate category if provided
    if "category" in update_data:
        category = update_data["category"].lower().strip()
        if category not in VALID_CATEGORIES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid category. Must be one of: {', '.join(sorted(VALID_CATEGORIES))}",
            )
        update_data["category"] = category

    entry = await changelog_ops.update_entry(db, entry, update_data)
    await db.commit()

    # Re-fetch with commits loaded
    refreshed = await changelog_ops.get(db, entry.id)
    if refreshed is None:
        raise HTTPException(status_code=404, detail="Changelog entry not found after update")
    return _serialize_entry(refreshed)


@router.delete(
    "/entries/{entry_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_changelog_entry(
    entry_id: uuid_pkg.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> None:
    """Delete a changelog entry (cascade-deletes linked commits).

    Deleted commits become available for re-processing in future generations.
    Requires editor access.
    """
    entry = await changelog_ops.get(db, entry_id)
    if not entry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Changelog entry not found",
        )

    await check_product_editor_access(db, entry.product_id, current_user.id)

    await changelog_ops.delete_entry(db, entry)
    await db.commit()


# ---------------------------------------------------------------------------
# Background task
# ---------------------------------------------------------------------------


async def _run_changelog_generation(
    product_id: str,
    user_id: str,
    github_token: str,
) -> None:
    """Background task for changelog generation.

    Uses a fresh DB session to avoid statement timeout on long-running AI operations.
    """
    from app.core.database import async_session_maker
    from app.services.changelog import ChangelogGenerator
    from app.services.github import GitHubReadOperations

    async with async_session_maker() as db:
        try:
            product_uuid = uuid_pkg.UUID(product_id)
            user_uuid = uuid_pkg.UUID(user_id)

            product = await product_ops.get(db, product_uuid)
            if not product:
                logger.warning(f"Product {product_id} not found for changelog generation")
                return

            repos = await repository_ops.get_github_repos_by_product(db, product_id=product_uuid)
            if not repos:
                logger.warning(f"No repos found for product {product_id}")
                return

            github = GitHubReadOperations(github_token)

            generator = ChangelogGenerator(
                product=product,
                repos=repos,
                github=github,
                user_id=user_uuid,
            )

            result = await generator.generate(db)

            logger.info(
                f"Changelog generation completed for product {product_id}: "
                f"{result.entries_created} entries from {result.commits_processed} commits "
                f"({result.batches_completed}/{result.batches_total} batches)"
            )

            if result.errors:
                logger.warning(
                    f"Changelog generation had {len(result.errors)} batch errors: {result.errors}"
                )

        except Exception as e:
            logger.error(f"Changelog generation failed for product {product_id}: {e}")
            # Clear progress on failure
            try:
                product = await product_ops.get(db, uuid_pkg.UUID(product_id))
                if product:
                    progress = product.docs_generation_progress
                    if isinstance(progress, dict) and progress.get("type") == "changelog":
                        product.docs_generation_progress = None
                        await db.commit()
            except Exception:
                pass
