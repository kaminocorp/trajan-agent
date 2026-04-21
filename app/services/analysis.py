"""
Analysis background task for AI-powered product analysis.

This module provides the background task entry point for product analysis.
The actual analysis workflow is coordinated by AnalysisOrchestrator.

Part of the Analysis Agent refactoring (Phase 5).
"""

import logging
import uuid as uuid_pkg

from app.core.database import async_session_maker
from app.core.rls import set_rls_user_context
from app.models.product import Product
from app.schemas.product_overview import ProductOverview
from app.services.analysis_orchestrator import AnalysisOrchestrator
from app.services.docs.file_source import create_github_service_factory, get_fallback_github_service

logger = logging.getLogger(__name__)


async def run_analysis_task(
    product_id: str,
    user_id: str,
) -> None:
    """
    Background task that runs the analysis and updates the product.

    This function creates its own database session since FastAPI's
    request session is closed by the time background tasks run.

    Security: GitHub token is fetched inside this task rather than passed
    as a parameter to avoid token exposure in logs or error dumps.

    Workflow:
    1. Fetch product and GitHub token
    2. Create AnalysisOrchestrator
    3. Run orchestrated analysis (stats + architecture in parallel, then content)
    4. Store results and update status

    Args:
        product_id: UUID of the product to analyze
        user_id: UUID of the user (for data isolation)
    """
    logger.info(f"Background analysis task started for product {product_id}")

    user_uuid = uuid_pkg.UUID(user_id)

    async with async_session_maker() as session:
        product = None
        try:
            # Fresh session → must set RLS context before any RLS-protected query.
            await set_rls_user_context(session, user_uuid)

            # Get the product
            product = await session.get(Product, uuid_pkg.UUID(product_id))
            if not product:
                logger.error(f"Product not found: {product_id}")
                return

            # Create per-repo token resolution factory (per-repo token > App > PAT)
            factory = await create_github_service_factory(session, user_uuid)

            # Get fallback service for non-repo-specific API calls
            fallback_service = await get_fallback_github_service(session, user_uuid)

            # Run orchestrated analysis with per-repo token resolution
            orchestrator = AnalysisOrchestrator(
                session,
                product,
                user_uuid,
                github_service_factory=factory,
                github_service=fallback_service,
            )
            overview = await orchestrator.analyze_product()

            # Update product with results using fresh session to avoid statement timeout.
            # The session has been open for the entire AI analysis process.
            await _mark_analysis_completed(product_id, overview, user_id)
            logger.info(f"Analysis completed successfully for product {product_id}")

        except Exception as e:
            logger.exception(f"Analysis failed for product {product_id}: {e}")
            await _mark_analysis_failed(product_id, str(e), user_id)
            raise


async def _mark_analysis_completed(
    product_id: str, overview: ProductOverview, user_id: str
) -> None:
    """Mark analysis as completed using a fresh session to avoid statement timeout."""
    try:
        async with async_session_maker() as session:
            # Fresh session → must set RLS context before any RLS-protected query.
            await set_rls_user_context(session, uuid_pkg.UUID(user_id))
            product = await session.get(Product, uuid_pkg.UUID(product_id))
            if product:
                product.product_overview = overview.model_dump(mode="json")
                product.analysis_status = "completed"
                product.analysis_error = None
                product.analysis_progress = None
                await session.commit()
    except Exception as e:
        logger.error(f"Failed to mark analysis as completed for product {product_id}: {e}")


async def _mark_analysis_failed(product_id: str, error_message: str, user_id: str) -> None:
    """Mark analysis as failed using a fresh session to avoid statement timeout."""
    try:
        async with async_session_maker() as session:
            # Fresh session → must set RLS context before any RLS-protected query.
            await set_rls_user_context(session, uuid_pkg.UUID(user_id))
            product = await session.get(Product, uuid_pkg.UUID(product_id))
            if product:
                product.analysis_status = "failed"
                product.analysis_error = error_message[:500]
                product.analysis_progress = None
                await session.commit()
    except Exception as e:
        logger.error(f"Failed to mark analysis as failed for product {product_id}: {e}")
