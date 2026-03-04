"""GitHub App webhook handler.

Receives events from GitHub when:
- App is installed/uninstalled on an org
- Repos are added/removed from an installation
- Installation is suspended/unsuspended

No authentication required (validated via HMAC-SHA256 webhook signature).
Uses DbSession (raw, no RLS) since there is no user context.
"""

import hashlib
import hmac
import logging

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import DbSession
from app.config import settings
from app.core.audit import log_installation_event, log_webhook_received
from app.domain import github_app_installation_ops, github_app_installation_repo_ops

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _verify_signature(body: bytes, signature: str | None) -> bool:
    """Verify GitHub webhook HMAC-SHA256 signature."""
    if not signature or not settings.github_app_webhook_secret:
        return False
    expected = "sha256=" + hmac.new(
        settings.github_app_webhook_secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/github")
async def github_webhook(request: Request, db: DbSession) -> dict:
    """Handle incoming GitHub App webhook events."""
    body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256")

    if not _verify_signature(body, signature):
        raise HTTPException(401, "Invalid webhook signature")

    event = request.headers.get("X-GitHub-Event")
    payload = await request.json()
    action = payload.get("action")

    log_webhook_received(event or "", action)

    match event:
        case "installation":
            await _handle_installation(db, payload)
        case "installation_repositories":
            await _handle_repo_change(db, payload)

    return {"ok": True}


async def _handle_installation(db: AsyncSession, payload: dict) -> None:
    """Handle app install/uninstall/suspend/unsuspend."""
    action = payload.get("action")
    installation_data = payload.get("installation")
    if not action or not installation_data:
        logger.warning("Malformed installation webhook — missing action or installation data")
        return
    installation_id = installation_data.get("id")
    if not installation_id:
        logger.warning("Malformed installation webhook — missing installation id")
        return

    match action:
        case "created":
            # Installation record is created via the /link endpoint (Step 12)
            # where we have the org_id context from the OAuth state param.
            account = installation_data.get("account", {})
            logger.info(
                f"GitHub App installed: {installation_id} "
                f"on {account.get('login', 'unknown')}"
            )

        case "deleted":
            log_installation_event("deleted", installation_id)
            await github_app_installation_ops.delete_by_installation_id(
                db, installation_id
            )

        case "suspend":
            log_installation_event("suspend", installation_id)
            await github_app_installation_ops.suspend(db, installation_id)

        case "unsuspend":
            log_installation_event("unsuspend", installation_id)
            await github_app_installation_ops.unsuspend(db, installation_id)


async def _handle_repo_change(db: AsyncSession, payload: dict) -> None:
    """Handle repos added/removed from an installation."""
    installation_data = payload.get("installation")
    if not installation_data:
        logger.warning("Malformed repo change webhook — missing installation data")
        return
    installation_id = installation_data.get("id")
    if not installation_id:
        logger.warning("Malformed repo change webhook — missing installation id")
        return

    installation = await github_app_installation_ops.get_by_installation_id(
        db, installation_id
    )
    if not installation:
        logger.warning(
            f"Webhook for unknown installation {installation_id} — ignoring"
        )
        return

    for repo in payload.get("repositories_added", []):
        await github_app_installation_repo_ops.upsert(
            db,
            installation_db_id=installation.id,
            github_repo_id=repo["id"],
            full_name=repo["full_name"],
        )
        logger.info(f"Repo added to installation: {repo['full_name']}")

    for repo in payload.get("repositories_removed", []):
        await github_app_installation_repo_ops.delete_by_github_id(
            db,
            installation_db_id=installation.id,
            github_repo_id=repo["id"],
        )
        logger.info(f"Repo removed from installation: {repo['full_name']}")
