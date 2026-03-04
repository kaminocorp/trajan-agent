"""Custom document generation API endpoints.

Endpoints for generating custom documentation based on user requests with
specific parameters (doc type, format style, target audience).

Supports both synchronous generation (for smaller requests) and background
generation with progress tracking (for longer requests).
"""

import asyncio
import logging
import uuid as uuid_pkg
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    SubscriptionContext,
    check_product_editor_access,
    get_current_user,
    get_db_with_rls,
    require_product_subscription,
)
from app.domain import preferences_ops, product_ops, repository_ops
from app.models.custom_doc_job import CustomDocJob
from app.models.user import User
from app.schemas.docs import (
    CustomDocRequestSchema,
    CustomDocResponseSchema,
    CustomDocStatusSchema,
)
from app.services.docs import job_store
from app.services.docs.custom_generator import CustomDocGenerator
from app.services.docs.types import CustomDocRequest
from app.services.github import GitHubService

logger = logging.getLogger(__name__)

# Rate limit configuration by plan tier
RATE_LIMITS = {
    "none": {"max_requests": 5, "window_hours": 24},
    "indie": {"max_requests": 25, "window_hours": 24},
    "pro": {"max_requests": 100, "window_hours": 24},
    "scale": {"max_requests": 1000, "window_hours": 24},
}


async def check_custom_doc_rate_limit(
    ctx: SubscriptionContext,
    current_user: User,
    db: AsyncSession,
) -> None:
    """Check custom doc generation rate limit using the product's org tier.

    Uses the subscription context from the product's organization (not the user's
    default org) to determine the correct rate limit tier.
    """
    tier = ctx.subscription.plan_tier
    limits = RATE_LIMITS.get(tier, RATE_LIMITS["none"])

    window_start = datetime.now(UTC) - timedelta(hours=limits["window_hours"])

    # Count recent jobs for this user
    result = await db.execute(
        select(func.count())
        .select_from(CustomDocJob)
        .where(CustomDocJob.user_id == current_user.id)  # type: ignore[arg-type]
        .where(CustomDocJob.created_at >= window_start)  # type: ignore[arg-type]
    )
    count = result.scalar() or 0

    if count >= limits["max_requests"]:
        # Get oldest job in window to calculate reset time
        oldest_result = await db.execute(
            select(func.min(CustomDocJob.created_at))
            .where(CustomDocJob.user_id == current_user.id)  # type: ignore[arg-type]
            .where(CustomDocJob.created_at >= window_start)  # type: ignore[arg-type]
        )
        oldest_job_time = oldest_result.scalar()

        # Calculate approximate reset time (when oldest job falls out of window)
        if oldest_job_time:
            reset_at = oldest_job_time + timedelta(hours=limits["window_hours"])
        else:
            reset_at = datetime.now(UTC) + timedelta(hours=limits["window_hours"])

        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "code": "RATE_LIMIT_EXCEEDED",
                "message": "Generation limit reached",
                "current_plan": tier,
                "plan_display_name": ctx.plan.display_name,
                "limit": limits["max_requests"],
                "used": count,
                "window_hours": limits["window_hours"],
                "reset_at": reset_at.isoformat(),
            },
        )


async def generate_custom_document(
    product_id: uuid_pkg.UUID,
    request: CustomDocRequestSchema,
    background: bool = Query(False, description="Run generation in background with progress"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> CustomDocResponseSchema:
    """
    Generate a custom document based on user request.

    If background=False (default): Generates synchronously and returns content.
    If background=True: Starts background job and returns job_id for polling.

    Args:
        product_id: The product to generate documentation for
        request: The custom doc request with prompt and parameters
        background: If True, run async and return job ID
        current_user: The authenticated user
        db: Database session

    Returns:
        CustomDocResponseSchema with generated content or job_id
    """
    sub_ctx = await require_product_subscription(db, product_id)
    product = sub_ctx.product

    # Check rate limit using the product's org tier (not user's default org)
    await check_custom_doc_rate_limit(sub_ctx, current_user, db)

    # Get user's GitHub token (decrypted)
    preferences = await preferences_ops.get_by_user_id(db, current_user.id)
    github_token = preferences_ops.get_decrypted_token(preferences) if preferences else None
    if not github_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="GitHub token not configured. Please add your GitHub token in Settings.",
        )

    # Get repositories for the product (RLS enforces access)
    repositories = await repository_ops.get_by_product(db, product_id=product_id)

    if not repositories:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No repositories linked to this product. Add a repository first.",
        )

    # Convert Pydantic schema to dataclass
    custom_request = CustomDocRequest(
        prompt=request.prompt,
        doc_type=request.doc_type,
        format_style=request.format_style,
        target_audience=request.target_audience,
        focus_paths=request.focus_paths,
        title=request.title,
    )

    if background:
        # Create job in database
        job_id = await job_store.create_job(
            db,
            product_id=str(product_id),
            user_id=str(current_user.id),
        )

        # Run generation in background task — pass primitive IDs only
        # to avoid stale ORM objects after the request session closes
        asyncio.create_task(
            _run_background_generation(
                job_id=job_id,
                custom_request=custom_request,
                product_id=product_id,
                repository_ids=[r.id for r in repositories],
                user_id=current_user.id,
                github_token=github_token,
            )
        )

        return CustomDocResponseSchema(
            status="generating",
            job_id=job_id,
        )
    else:
        # Synchronous generation
        github_service = GitHubService(github_token)
        generator = CustomDocGenerator(db, github_service)

        result = await generator.generate(
            request=custom_request,
            product=product,
            repositories=repositories,
            user_id=current_user.id,
            save_immediately=False,
        )

        if result.success:
            return CustomDocResponseSchema(
                status="completed",
                content=result.content,
                suggested_title=result.suggested_title,
                generation_time_seconds=result.generation_time_seconds,
            )
        else:
            return CustomDocResponseSchema(
                status="failed",
                error=result.error,
            )


async def _run_background_generation(
    job_id: str,
    custom_request: CustomDocRequest,
    product_id: uuid_pkg.UUID,
    repository_ids: list[uuid_pkg.UUID],
    user_id: uuid_pkg.UUID,
    github_token: str,
) -> None:
    """
    Run custom doc generation in background with progress updates.

    Accepts only primitive IDs (not ORM objects) to avoid DetachedInstanceError
    from stale objects bound to the request's closed session.
    """
    from app.core.database import async_session_maker

    async def progress_callback(stage: str) -> None:
        async with async_session_maker() as db:
            await job_store.update_progress(db, job_id, stage)

    async def check_cancelled() -> bool:
        async with async_session_maker() as db:
            return await job_store.is_cancelled(db, job_id)

    try:
        # Create a new database session and re-fetch ORM objects
        async with async_session_maker() as db:
            product = await product_ops.get(db, product_id)
            if not product:
                raise ValueError(f"Product {product_id} not found")

            repositories = [
                repo
                for rid in repository_ids
                if (repo := await repository_ops.get(db, rid)) is not None
            ]

            github_service = GitHubService(github_token)
            generator = CustomDocGenerator(db, github_service)

            result = await generator.generate(
                request=custom_request,
                product=product,
                repositories=repositories,
                user_id=user_id,
                save_immediately=False,
                progress_callback=progress_callback,
                cancellation_check=check_cancelled,
            )

            if result.success:
                await job_store.set_completed(
                    db,
                    job_id,
                    content=result.content or "",
                    suggested_title=result.suggested_title or "Untitled Document",
                )
            elif result.error == "Cancelled by user":
                # Already marked as cancelled, nothing to do
                pass
            else:
                await job_store.set_failed(db, job_id, result.error or "Unknown error")

    except Exception as e:
        logger.exception(f"Background generation failed for job {job_id}")
        async with async_session_maker() as db:
            await job_store.set_failed(db, job_id, str(e))


async def get_custom_doc_status(
    product_id: uuid_pkg.UUID,
    job_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> CustomDocStatusSchema:
    """
    Get the status of a background custom document generation job.

    Poll this endpoint to check progress and get the result when complete.

    Args:
        product_id: The product ID (for authorization)
        job_id: The job ID returned from generate endpoint
        current_user: The authenticated user
        db: Database session

    Returns:
        CustomDocStatusSchema with current status, progress, and content when done
    """
    job = await job_store.get_job(db, job_id)

    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found or expired",
        )

    # Verify the job belongs to this user and product
    if job.user_id != current_user.id or job.product_id != product_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to access this job",
        )

    return CustomDocStatusSchema(
        status=job.status,
        progress=job.progress,
        content=job.content,
        suggested_title=job.suggested_title,
        error=job.error,
    )


async def cancel_custom_doc_job(
    product_id: uuid_pkg.UUID,
    job_id: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> dict[str, bool]:
    """
    Cancel a running custom document generation job.

    This marks the job as cancelled and stops further processing.

    Args:
        product_id: The product ID (for authorization)
        job_id: The job ID to cancel
        current_user: The authenticated user
        db: Database session

    Returns:
        {"cancelled": True} if job was cancelled, {"cancelled": False} otherwise
    """
    await require_product_subscription(db, product_id)

    job = await job_store.get_job(db, job_id)

    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found or expired",
        )

    # Verify the job belongs to this user and product
    if job.user_id != current_user.id or job.product_id != product_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to access this job",
        )

    cancelled = await job_store.set_cancelled(db, job_id)
    return {"cancelled": cancelled}


async def generate_assessment(
    product_id: uuid_pkg.UUID,
    assessment_type: str,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> dict[str, Any]:
    """
    Generate a critical assessment of the codebase.

    This endpoint generates honest, scored assessments of code quality,
    security, or performance. Assessments are saved automatically to the
    appropriate technical documentation subsection.

    Args:
        product_id: The product to assess
        assessment_type: One of "code-quality", "security", "performance"
        current_user: The authenticated user
        db: Database session

    Returns:
        The saved assessment document
    """
    sub_ctx = await require_product_subscription(db, product_id)

    # Require editor access — viewers cannot trigger assessment generation
    await check_product_editor_access(db, product_id, current_user.id)

    # Check rate limit using the product's org tier (not user's default org)
    await check_custom_doc_rate_limit(sub_ctx, current_user, db)

    # Validate assessment type
    valid_types = ["code-quality", "security", "performance"]
    if assessment_type not in valid_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid assessment type. Must be one of: {', '.join(valid_types)}",
        )

    product = sub_ctx.product

    # Get user's GitHub token (decrypted)
    preferences = await preferences_ops.get_by_user_id(db, current_user.id)
    github_token = preferences_ops.get_decrypted_token(preferences) if preferences else None
    if not github_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="GitHub token not configured. Please add your GitHub token in Settings.",
        )

    # Get repositories for the product
    repositories = await repository_ops.get_by_product(db, product_id=product_id)
    if not repositories:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No repositories linked to this product. Add a repository first.",
        )

    # Generate the assessment
    github_service = GitHubService(github_token)
    generator = CustomDocGenerator(db, github_service)

    result = await generator.generate_assessment(
        assessment_type=assessment_type,
        product=product,
        repositories=repositories,
        user_id=current_user.id,
    )

    if result.success and result.document:
        # Import serialize_document from crud module
        from app.api.v1.documents.crud import serialize_document

        return serialize_document(result.document)
    else:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=result.error or "Assessment generation failed",
        )


