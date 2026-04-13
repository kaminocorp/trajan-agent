"""Internal API endpoints — protected by shared secret, not user auth.

These endpoints are primarily for manual triggering and debugging.
Scheduled jobs run automatically via APScheduler (see services/scheduler.py).
Endpoints bypass Supabase JWT auth and instead validate a shared secret via X-Cron-Secret.
"""

import hmac
import logging
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import settings
from app.core.database import get_db

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


@router.post("/auto-progress")
async def trigger_auto_progress(
    x_cron_secret: str = Header(...),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Manually trigger auto-progress generation for all eligible organizations.

    Protected by X-Cron-Secret header. Useful for testing or forcing a refresh.
    Note: This job runs automatically via APScheduler — see services/scheduler.py.
    """
    _verify_cron_secret(x_cron_secret)

    from app.services.progress.auto_generator import auto_progress_generator

    report = await auto_progress_generator.run_for_all_orgs(db)
    await db.commit()

    return asdict(report)


@router.post("/send-plan-prompt-emails")
async def trigger_plan_prompt_emails(
    x_cron_secret: str = Header(...),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Manually trigger plan-selection prompt emails for orgs without a plan.

    Protected by X-Cron-Secret header. Useful for testing or forcing a send.
    Note: This job runs automatically via APScheduler — see services/scheduler.py.
    """
    _verify_cron_secret(x_cron_secret)

    from app.services.email.plan_prompt import send_plan_selection_prompts

    report = await send_plan_selection_prompts(db)
    await db.commit()

    return asdict(report)


@router.post("/send-weekly-digest")
async def trigger_weekly_digest(
    x_cron_secret: str = Header(...),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Manually trigger the weekly digest email for all opted-in users.

    Protected by X-Cron-Secret header. Useful for testing or forcing a send.
    Note: This job runs automatically via APScheduler — see services/scheduler.py.
    """
    _verify_cron_secret(x_cron_secret)

    from app.services.email.weekly_digest import send_digests

    report = await send_digests(db, frequency="weekly")
    await db.commit()

    return asdict(report)


@router.post("/send-daily-digest")
async def trigger_daily_digest(
    x_cron_secret: str = Header(...),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """
    Manually trigger the daily digest email for all opted-in users.

    Protected by X-Cron-Secret header. Useful for testing or forcing a send.
    Note: This job runs automatically via APScheduler — see services/scheduler.py.
    """
    _verify_cron_secret(x_cron_secret)

    from app.services.email.weekly_digest import send_digests

    report = await send_digests(db, frequency="daily")
    await db.commit()

    return asdict(report)
