"""Internal API endpoints — protected by shared secret, not user auth.

These endpoints are primarily for manual triggering and debugging.
Scheduled jobs run automatically via APScheduler (see services/scheduler.py).
Endpoints bypass Supabase JWT auth and instead validate a shared secret via X-Cron-Secret.

**Phase 3f refactor (cron-role bypass-then-scope plan):** every endpoint
delegates to ``scheduler.trigger_now(<job_id>)``. The full bypass-then-scope
discipline (bootstrap on ``cron_session_maker`` → per-tenant scoped session
with RLS context) lives in ``services/scheduler.py`` — these HTTP surfaces
no longer open their own DB sessions. Rationale: any difference between
the manual-trigger path and the APScheduler path would defeat the purpose
of having a manual trigger.
"""

import hmac
import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, status

from app.config.settings import settings
from app.services.scheduler import scheduler

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])


def _verify_cron_secret(x_cron_secret: str = Header(...)) -> None:
    """Validate the X-Cron-Secret header against the configured secret."""
    if not settings.cron_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Cron secret not configured",
        )
    if not hmac.compare_digest(x_cron_secret, settings.cron_secret):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid cron secret",
        )


def _lock_held_response() -> dict[str, Any]:
    """Returned when ``trigger_now`` returns None — advisory lock held by
    another instance (expected during multi-instance deploys)."""
    return {"status": "skipped", "reason": "lock held by another instance"}


@router.post("/auto-progress")
async def trigger_auto_progress(
    x_cron_secret: str = Header(...),
) -> dict[str, Any]:
    """Manually trigger auto-progress generation for every eligible org.

    Delegates to :meth:`Scheduler.trigger_now` so the HTTP path runs the
    exact same bypass-then-scope flow as the hourly APScheduler run.
    """
    _verify_cron_secret(x_cron_secret)
    result = await scheduler.trigger_now("auto_progress")
    return result or _lock_held_response()


@router.post("/send-plan-prompt-emails")
async def trigger_plan_prompt_emails(
    x_cron_secret: str = Header(...),
) -> dict[str, Any]:
    """Manually trigger plan-selection prompt emails for orgs without a plan."""
    _verify_cron_secret(x_cron_secret)
    result = await scheduler.trigger_now("plan_prompt_emails")
    return result or _lock_held_response()


@router.post("/send-weekly-digest")
async def trigger_weekly_digest(
    x_cron_secret: str = Header(...),
) -> dict[str, Any]:
    """Manually trigger the weekly digest email for all opted-in users."""
    _verify_cron_secret(x_cron_secret)
    result = await scheduler.trigger_now("weekly_digest")
    return result or _lock_held_response()


@router.post("/send-daily-digest")
async def trigger_daily_digest(
    x_cron_secret: str = Header(...),
) -> dict[str, Any]:
    """Manually trigger the daily digest email for all opted-in users."""
    _verify_cron_secret(x_cron_secret)
    result = await scheduler.trigger_now("daily_digest")
    return result or _lock_held_response()
