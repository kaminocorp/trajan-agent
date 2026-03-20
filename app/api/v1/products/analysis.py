"""Product analysis: AI-powered repository analysis."""

import asyncio
import logging
import uuid as uuid_pkg
from datetime import UTC, datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    check_product_editor_access,
    get_current_user,
    get_db_with_rls,
    require_product_subscription,
)
from app.api.v1.products.docs_generation import _has_github_access
from app.core.database import async_session_maker
from app.core.rls import set_rls_user_context
from app.domain import product_ops
from app.domain.preferences_operations import preferences_ops
from app.domain.repository_operations import repository_ops
from app.domain.subscription_operations import subscription_ops
from app.models.product import Product
from app.models.user import User
from app.schemas.product_overview import AnalyzeProductResponse
from app.services.analysis import run_analysis_task

logger = logging.getLogger(__name__)

router = APIRouter()

# Analysis frequency limits in hours
ANALYSIS_FREQUENCY_LIMITS = {
    "weekly": 7 * 24,  # 168 hours
    "daily": 24,  # 24 hours
    "realtime": 0,  # No limit
}


@router.post("/{product_id}/analyze", response_model=AnalyzeProductResponse)
async def analyze_product(
    product_id: uuid_pkg.UUID,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> AnalyzeProductResponse:
    """
    Trigger AI analysis of the product's repositories.

    Requires Editor or Admin access to the product.

    Additional requirements:
    - Agent to be enabled for the organization (free tier must be within repo limit)
    - Analysis frequency must be respected (weekly for Observer, daily for Foundations)

    Analysis runs in the background. Poll GET /products/{id} for status updates.
    The `analysis_status` field will be:
    - "analyzing" while in progress
    - "completed" when done (product_overview will contain results)
    - "failed" if an error occurred
    """
    # Check product access first (verifies user has editor access via org membership)
    await check_product_editor_access(db, product_id, current_user.id)

    # Get subscription context for the product's organization (not user's default org)
    # Also checks subscription is active — raises 402 if pending/none
    # product is included in sub_ctx (avoids redundant DB lookup)
    sub_ctx = await require_product_subscription(db, product_id)
    product = sub_ctx.product

    # Check if agent features are enabled for this organization
    repo_count = await repository_ops.count_by_org(db, sub_ctx.organization.id)
    is_agent_enabled = await subscription_ops.is_agent_enabled(
        db, sub_ctx.organization.id, repo_count
    )
    if not is_agent_enabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Agent disabled. The organization has {repo_count} repositories but the "
            f"{sub_ctx.plan.display_name} plan only allows {sub_ctx.plan.base_repo_limit}. "
            "Remove repositories or upgrade to re-enable.",
        )

    # Check if already analyzing
    if product.analysis_status == "analyzing":
        return AnalyzeProductResponse(
            status="already_analyzing",
            message="Analysis already in progress. Poll GET /products/{id} for status.",
        )

    # Check analysis frequency limit
    frequency_limit_hours = ANALYSIS_FREQUENCY_LIMITS.get(sub_ctx.plan.analysis_frequency, 0)
    if frequency_limit_hours > 0 and product.analysis_status == "completed":
        # Use updated_at as a proxy for when analysis was completed
        # (set when status changes to "completed")
        last_analysis_time = product.updated_at
        if last_analysis_time:
            hours_since_last = (datetime.now(UTC) - last_analysis_time).total_seconds() / 3600
            if hours_since_last < frequency_limit_hours:
                hours_remaining = int(frequency_limit_hours - hours_since_last)
                frequency_display = (
                    "once per week"
                    if sub_ctx.plan.analysis_frequency == "weekly"
                    else "once per day"
                )
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Analysis limited to {frequency_display} on {sub_ctx.plan.display_name} plan. "
                    f"Next analysis available in {hours_remaining} hours.",
                )

    # Check that at least one form of GitHub access exists (PAT, App, or per-repo token).
    # The background task uses TokenResolver to resolve the best token per-repo.
    if not await _has_github_access(db, product_id, current_user.id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No GitHub access configured. Install the GitHub App, "
            "add a Personal Access Token, or link repos with a fine-grained token.",
        )

    # Update status to analyzing using fresh session.
    # The main session has been open during validation queries above; Supabase's statement
    # timeout may cancel the UPDATE if the transaction has been open too long.
    # Using a fresh session resets the transaction timer. (See: github-import-statement-timeout-fix)
    async with async_session_maker() as update_session:
        await set_rls_user_context(update_session, current_user.id)
        update_product = await update_session.get(Product, product_id)
        if update_product:
            update_product.analysis_status = "analyzing"
            await update_session.commit()
            logger.info(f"Analysis triggered for product {product_id}")
        else:
            logger.warning(f"Product {product_id} not found during status update")

    # Dispatch background task
    # Note: run_analysis_task creates its own database session since
    # FastAPI's request session is closed by the time background tasks run.
    # Security: GitHub token is fetched inside the task, not passed as param.
    background_tasks.add_task(
        run_analysis_task,
        product_id=str(product.id),
        user_id=str(current_user.id),
    )

    return AnalyzeProductResponse(
        status="analyzing",
        message="Analysis started. Poll GET /products/{id} for status updates.",
    )


async def maybe_auto_trigger_analysis(
    product_id: uuid_pkg.UUID,
    user_id: uuid_pkg.UUID,
    db: AsyncSession,
) -> bool:
    """Check user preference and preconditions, then trigger analysis if appropriate.

    Returns True if analysis was triggered, False otherwise.

    Uses a fresh database session for the status UPDATE to ensure the commit happens.
    Mirrors the maybe_auto_trigger_docs() pattern in docs_generation.py.
    Skips access checks and frequency limits (user just created the project).
    """
    # 1. Check user's auto_generate_docs preference (reused — no separate setting)
    prefs = await preferences_ops.get_or_create(db, user_id)
    if not prefs.auto_generate_docs:
        return False

    # 2. Check any form of GitHub access exists (PAT, App installation, or per-repo token)
    if not await _has_github_access(db, product_id, user_id):
        logger.debug(f"Skipping auto-analysis for product {product_id}: no GitHub access configured")
        return False

    # 3. Check product exists and is not already analyzing
    product = await product_ops.get(db, product_id)
    if not product:
        return False

    if product.analysis_status == "analyzing":
        logger.debug(
            f"Skipping auto-analysis for product {product_id}: analysis already in progress"
        )
        return False

    # 4. Update status using fresh session (ensures commit + avoids statement timeout)
    async with async_session_maker() as update_session:
        await set_rls_user_context(update_session, user_id)
        update_product = await update_session.get(Product, product_id)
        if update_product:
            update_product.analysis_status = "analyzing"
            await update_session.commit()
            logger.info(f"Auto-triggered analysis for product {product_id}")
        else:
            logger.warning(f"Product {product_id} not found during analysis auto-trigger")
            return False

    # 5. Start as independent async task (runs concurrently with docs generation)
    asyncio.create_task(
        run_analysis_task(
            product_id=str(product_id),
            user_id=str(user_id),
        ),
        name=f"analysis-{product_id}",
    )

    return True
