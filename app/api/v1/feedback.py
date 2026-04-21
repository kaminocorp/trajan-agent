"""Feedback API endpoints for bug reports and feature requests."""

import logging
import uuid as uuid_pkg
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_with_rls
from app.core.database import async_session_maker
from app.core.rls import set_rls_user_context
from app.domain.feedback_operations import feedback_ops
from app.models.feedback import Feedback, FeedbackCreate, FeedbackRead
from app.models.user import User
from app.services.interpreter import FeedbackInterpreter

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/feedback", tags=["feedback"])


def _feedback_to_dict(f: Feedback) -> dict[str, Any]:
    """Convert Feedback model to response dict."""
    return {
        "id": str(f.id),
        "type": f.type,
        "tags": f.tags or [],
        "severity": f.severity,
        "title": f.title,
        "description": f.description,
        "ai_summary": f.ai_summary,
        "page_url": f.page_url,
        "status": f.status,
        "created_at": f.created_at.isoformat(),
    }


@router.post("", response_model=FeedbackRead, status_code=status.HTTP_201_CREATED)
async def submit_feedback(
    data: FeedbackCreate,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> dict[str, Any]:
    """Submit user feedback. AI interpretation runs in background."""
    feedback = await feedback_ops.create_feedback(db, user_id=current_user.id, data=data)

    # Process AI interpretation in background
    background_tasks.add_task(process_ai_interpretation, feedback.id, current_user.id)

    return _feedback_to_dict(feedback)


@router.get("", response_model=list[FeedbackRead])
async def list_my_feedback(
    skip: int = 0,
    limit: int = 100,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> list[dict[str, Any]]:
    """List feedback submitted by the current user."""
    items = await feedback_ops.list_by_user(db, user_id=current_user.id, skip=skip, limit=limit)
    return [_feedback_to_dict(f) for f in items]


@router.get("/{feedback_id}", response_model=FeedbackRead)
async def get_feedback(
    feedback_id: uuid_pkg.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> dict[str, Any]:
    """Get a single feedback item."""
    # Use base class get_by_user which takes user_id and id
    feedback = await feedback_ops.get_by_user(db, user_id=current_user.id, id=feedback_id)
    if not feedback:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Feedback not found",
        )
    return _feedback_to_dict(feedback)


async def process_ai_interpretation(feedback_id: uuid_pkg.UUID, user_id: uuid_pkg.UUID) -> None:
    """Background task to generate AI summary using modular interpreter.

    The RLS subject is the feedback submitter — they are the only user who can
    SELECT their own feedback row under the `feedback_select_own` policy.
    """
    async with async_session_maker() as db:
        try:
            # Fresh session → must set RLS context before any RLS-protected query.
            await set_rls_user_context(db, user_id)
            feedback = await feedback_ops.get(db, feedback_id)
            if feedback and not feedback.ai_summary:
                interpreter = FeedbackInterpreter()
                ai_summary = await interpreter.interpret(feedback)
                await feedback_ops.update_ai_summary(db, feedback_id, ai_summary)
                logger.info(f"AI interpretation completed for feedback {feedback_id}")
        except Exception as e:
            logger.error(f"AI interpretation failed for feedback {feedback_id}: {e}")
