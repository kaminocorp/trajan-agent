"""Product documentation generation: AI-powered docs creation."""

import asyncio
import logging
import uuid as uuid_pkg
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    check_product_editor_access,
    get_current_user,
    get_db_with_rls,
    require_product_subscription,
)
from app.domain import github_app_installation_ops, product_ops, repository_ops
from app.domain.organization_operations import organization_ops
from app.domain.preferences_operations import preferences_ops
from app.domain.product_access_operations import product_access_ops
from app.models.user import User
from app.schemas.docs import DocsStatusResponse, GenerateDocsRequest, GenerateDocsResponse
from app.services.github.app_auth import github_app_auth

router = APIRouter()
logger = logging.getLogger(__name__)

# Stale job detection: mark as failed if generating for longer than this
DOCS_GENERATION_TIMEOUT_MINUTES = 15


async def _has_github_access(
    db: AsyncSession,
    product_id: uuid_pkg.UUID,
    user_id: uuid_pkg.UUID,
) -> bool:
    """Check if there's any form of GitHub access for this product's repos.

    Returns True if any of these exist:
    1. User has a PAT in preferences
    2. Product's org has an active GitHub App installation
    3. Any repo in the product has a per-repo fine-grained token
    """
    # 1. User has PAT
    prefs = await preferences_ops.get_by_user_id(db, user_id)
    if prefs and preferences_ops.get_decrypted_token(prefs):
        return True

    # 2. Product's org has GitHub App installation
    if github_app_auth.is_configured:
        product = await product_ops.get(db, product_id)
        if product and product.organization_id:
            installation = await github_app_installation_ops.get_for_org(
                db, product.organization_id
            )
            if installation and not installation.suspended_at:
                return True

    # 3. Any repo has a per-repo token
    repos = await repository_ops.get_by_product(db, product_id)
    return any(getattr(repo, "encrypted_token", None) for repo in repos)


@router.post("/{product_id}/generate-docs", response_model=GenerateDocsResponse)
async def generate_documentation(
    product_id: uuid_pkg.UUID,
    background_tasks: BackgroundTasks,
    request: GenerateDocsRequest | None = None,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> GenerateDocsResponse:
    """
    Trigger DocumentOrchestrator to analyze and generate documentation.

    Requires Editor or Admin access to the product.

    Args:
        request: Optional request body with generation mode
            - mode="full": Regenerate all documentation from scratch (default)
            - mode="additive": Only add new docs, preserve existing

    Runs as a background task with progress updates. Poll GET /products/{id}/docs-status
    for real-time progress.
    """
    await check_product_editor_access(db, product_id, current_user.id)
    sub_ctx = await require_product_subscription(db, product_id)
    product = sub_ctx.product

    # Check if already running
    if product.docs_generation_status == "generating":
        return GenerateDocsResponse(
            status="already_running",
            message="Documentation generation already in progress",
        )

    # Check that at least one form of GitHub access exists (PAT, App, or per-repo token).
    # The background task uses TokenResolver to resolve the best token per-repo.
    if not await _has_github_access(db, product_id, current_user.id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No GitHub access configured. Install the GitHub App, "
            "add a Personal Access Token, or link repos with a fine-grained token.",
        )

    # Parse mode from request (default to "full")
    mode = request.mode if request else "full"

    # Update status
    product.docs_generation_status = "generating"
    product.docs_generation_error = None
    product.docs_generation_progress = None
    db.add(product)
    await db.commit()

    # Start background task
    background_tasks.add_task(
        run_document_orchestrator,
        product_id=str(product.id),
        user_id=str(current_user.id),
        mode=mode,
    )

    return GenerateDocsResponse(
        status="started",
        message=f"Documentation generation started (mode: {mode}). Poll for progress.",
    )


@router.get("/{product_id}/docs-status", response_model=DocsStatusResponse)
async def get_docs_generation_status(
    product_id: uuid_pkg.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> DocsStatusResponse:
    """Get current documentation generation status.

    Includes stale job detection: if a job has been "generating" for longer than
    DOCS_GENERATION_TIMEOUT_MINUTES, it's automatically marked as failed.
    """
    product = await product_ops.get(db, product_id)
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found",
        )

    # Check org membership and product access (at least viewer)
    if not product.organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found",
        )

    org_role = await organization_ops.get_member_role(db, product.organization_id, current_user.id)
    if not org_role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found",
        )

    access = await product_access_ops.get_effective_access(
        db, product_id, current_user.id, org_role
    )
    if access == "none":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found",
        )

    # Stale job detection: auto-fail jobs that have been running too long
    if product.docs_generation_status == "generating":
        progress = product.docs_generation_progress or {}
        updated_at_str = progress.get("updated_at")

        if updated_at_str:
            try:
                # Parse ISO format timestamp
                updated_at = datetime.fromisoformat(updated_at_str.replace("Z", "+00:00"))
                elapsed = datetime.now(UTC) - updated_at
                timeout_delta = timedelta(minutes=DOCS_GENERATION_TIMEOUT_MINUTES)

                if elapsed > timeout_delta:
                    # Job is stale - mark as failed
                    elapsed_minutes = int(elapsed.total_seconds() / 60)
                    stage = progress.get("stage", "unknown")
                    product.docs_generation_status = "failed"
                    product.docs_generation_error = (
                        f"Generation timed out after {elapsed_minutes} minutes "
                        f"(stuck on '{stage}' stage). Please try again."
                    )
                    product.docs_generation_progress = None
                    await db.commit()
                    logger.warning(
                        f"Auto-marked stale docs generation as failed for product {product_id}. "
                        f"Was stuck on '{stage}' for {elapsed_minutes} minutes."
                    )
            except (ValueError, TypeError) as e:
                # Couldn't parse timestamp - log but don't fail the request
                logger.warning(f"Failed to parse docs progress timestamp: {e}")
        else:
            # Fallback: no updated_at in progress, use product.updated_at as proxy
            # This catches cases where progress was never written (background task crash)
            if product.updated_at:
                # Ensure timezone-aware comparison
                product_updated = (
                    product.updated_at.replace(tzinfo=UTC)
                    if product.updated_at.tzinfo is None
                    else product.updated_at
                )
                elapsed = datetime.now(UTC) - product_updated
                timeout_delta = timedelta(minutes=DOCS_GENERATION_TIMEOUT_MINUTES)

                if elapsed > timeout_delta:
                    elapsed_minutes = int(elapsed.total_seconds() / 60)
                    product.docs_generation_status = "failed"
                    product.docs_generation_error = (
                        f"Generation timed out after {elapsed_minutes} minutes "
                        "(no progress recorded). Please try again."
                    )
                    product.docs_generation_progress = None
                    await db.commit()
                    logger.warning(
                        f"Auto-marked stale docs generation as failed for product {product_id}. "
                        f"No progress was recorded for {elapsed_minutes} minutes."
                    )

    return DocsStatusResponse(
        status=product.docs_generation_status or "idle",
        progress=product.docs_generation_progress,
        error=product.docs_generation_error,
        last_generated_at=product.last_docs_generated_at,
    )


@router.post("/{product_id}/reset-docs-generation", response_model=GenerateDocsResponse)
async def reset_docs_generation(
    product_id: uuid_pkg.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> GenerateDocsResponse:
    """
    Force-reset a stuck documentation generation job.

    This endpoint allows users to manually cancel a stuck generation job.
    The job will be marked as cancelled, allowing a new generation to be started.

    Requires Editor or Admin access to the product.
    """
    await check_product_editor_access(db, product_id, current_user.id)
    sub_ctx = await require_product_subscription(db, product_id)
    product = sub_ctx.product

    # Only reset if currently in generating state
    if product.docs_generation_status != "generating":
        return GenerateDocsResponse(
            status="not_generating",
            message="No generation in progress to reset.",
        )

    # Reset the stuck job
    product.docs_generation_status = "failed"
    product.docs_generation_error = "Generation was manually cancelled."
    product.docs_generation_progress = None
    await db.commit()

    logger.info(
        f"User {current_user.id} manually reset stuck docs generation for product {product_id}"
    )

    return GenerateDocsResponse(
        status="reset",
        message="Generation cancelled. You can now start a new generation.",
    )


async def _mark_generation_failed(
    product_id: str,
    error_message: str,
    max_retries: int = 3,
) -> None:
    """
    Helper to mark documentation generation as failed using a fresh DB session.

    This is used for error recovery when the main session may be in a bad state.
    Uses a fresh session to avoid Supabase statement timeout issues.
    Includes retry logic to handle transient connection failures.
    """
    from app.core.database import async_session_maker

    for attempt in range(max_retries):
        try:
            async with async_session_maker() as db:
                product_uuid = uuid_pkg.UUID(product_id)
                # Access was already verified when the task was started
                product = await product_ops.get(db, product_uuid)
                if product:
                    product.docs_generation_status = "failed"
                    product.docs_generation_error = error_message[:500]
                    product.docs_generation_progress = None
                    await db.commit()
                    logger.info(
                        f"Marked product {product_id} docs generation as failed: {error_message}"
                    )
                return  # Success - exit retry loop
        except Exception as db_error:
            if attempt < max_retries - 1:
                # Retry after brief pause
                logger.warning(
                    f"Retry {attempt + 1}/{max_retries} marking docs generation failed "
                    f"for product {product_id}: {db_error}"
                )
                await asyncio.sleep(1)
            else:
                # All retries exhausted - log critical error
                logger.critical(
                    f"CRITICAL: Failed to mark docs generation as failed after {max_retries} attempts. "
                    f"Product {product_id} may be stuck in 'generating' state. "
                    f"DB error: {db_error}. Original error: {error_message}"
                )


async def _mark_generation_completed(product_id: str) -> None:
    """
    Helper to mark documentation generation as completed using a fresh DB session.

    Uses a fresh session to avoid Supabase statement timeout issues. The transaction
    pooler (port 6543) has a statement timeout that cancels queries if the transaction
    has been open too long. Since AI operations can take minutes, we use a fresh session.
    """
    from app.core.database import async_session_maker

    try:
        async with async_session_maker() as db:
            product_uuid = uuid_pkg.UUID(product_id)
            product = await product_ops.get(db, product_uuid)
            if product:
                product.docs_generation_status = "completed"
                product.last_docs_generated_at = datetime.now(UTC)
                product.docs_generation_error = None
                product.docs_generation_progress = None
                await db.commit()
                logger.info(f"Documentation generation completed for product {product_id}")
    except Exception as db_error:
        logger.error(
            f"Failed to mark docs generation as completed for product {product_id}: {db_error}"
        )


async def maybe_auto_trigger_docs(
    product_id: uuid_pkg.UUID,
    user_id: uuid_pkg.UUID,
    db: AsyncSession,
) -> bool:
    """Check user preference and preconditions, then trigger docs generation if appropriate.

    Returns True if generation was triggered, False otherwise.

    Uses a fresh database session for the status UPDATE to ensure the commit happens.
    This is necessary because the calling code (github.py import endpoint) commits
    the repo imports before calling this function, leaving no subsequent commit
    for the status change. See: docs-auto-trigger-commit-fix (0.9.9).
    """
    from app.core.database import async_session_maker
    from app.core.rls import set_rls_user_context
    from app.models.product import Product

    # 1. Check user's auto_generate_docs preference
    prefs = await preferences_ops.get_or_create(db, user_id)
    if not prefs.auto_generate_docs:
        return False

    # 2. Check any form of GitHub access exists (PAT, App installation, or per-repo token)
    if not await _has_github_access(db, product_id, user_id):
        logger.debug(f"Skipping auto-trigger for product {product_id}: no GitHub access configured")
        return False

    # 3. Check product is not already generating
    product = await product_ops.get(db, product_id)
    if not product:
        return False

    if product.docs_generation_status == "generating":
        logger.debug(
            f"Skipping auto-trigger for product {product_id}: generation already in progress"
        )
        return False

    # 4. Update status using fresh session (ensures commit + avoids statement timeout)
    # Same pattern as analysis.py - the main session has already committed repo imports,
    # so we need a new transaction to persist the docs generation status.
    async with async_session_maker() as update_session:
        await set_rls_user_context(update_session, user_id)
        update_product = await update_session.get(Product, product_id)
        if update_product:
            update_product.docs_generation_status = "generating"
            update_product.docs_generation_error = None
            update_product.docs_generation_progress = None
            await update_session.commit()
            logger.info(f"Auto-triggered docs generation for product {product_id}")
        else:
            logger.warning(f"Product {product_id} not found during docs auto-trigger")
            return False

    # 5. Start as independent async task (runs concurrently with analysis)
    asyncio.create_task(
        run_document_orchestrator(
            product_id=str(product_id),
            user_id=str(user_id),
            mode="additive",
        ),
        name=f"docs-gen-{product_id}",
    )

    return True


async def run_document_orchestrator(
    product_id: str,
    user_id: str,
    mode: str = "full",
) -> None:
    """Background task to run documentation generation.

    Args:
        product_id: The product UUID
        user_id: The user UUID
        mode: Generation mode - "full" (regenerate all) or "additive" (only add new)
    """
    from app.core.database import async_session_maker
    from app.services.docs import DocumentOrchestrator
    from app.services.docs.file_source import (
        create_github_service_factory,
        get_fallback_github_service,
    )

    async with async_session_maker() as db:
        try:
            product_uuid = uuid_pkg.UUID(product_id)
            user_uuid = uuid_pkg.UUID(user_id)

            # Access was already verified when the task was started
            product = await product_ops.get(db, product_uuid)
            if not product:
                logger.warning(f"Product {product_id} not found for docs generation")
                return

            # Create per-repo token resolution factory
            # This allows each repo to use its own token (per-repo, App, or PAT)
            factory = await create_github_service_factory(db, user_uuid)

            # Get fallback service for sub-agents that haven't been refactored
            fallback_service = await get_fallback_github_service(db, user_uuid)

            # Run orchestrator with per-repo factory + optional fallback
            orchestrator = DocumentOrchestrator(
                db,
                product,
                github_service=fallback_service,
                github_service_factory=factory,
            )
            await orchestrator.run(mode=mode)

            # Update status on success using fresh session to avoid statement timeout
            # The orchestrator session has been open for the entire AI generation process
            await _mark_generation_completed(product_id)
            logger.info(
                f"Documentation generation completed for product {product_id} (mode: {mode})"
            )

        except Exception as e:
            logger.error(f"Documentation generation failed for product {product_id}: {e}")
            # Use a fresh session for error handling to avoid stale session issues
            await _mark_generation_failed(product_id, str(e))
