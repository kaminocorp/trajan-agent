"""GitHub App webhook handler.

Receives events from GitHub when:
- App is installed/uninstalled on an org
- Repos are added/removed from an installation
- Installation is suspended/unsuspended

No authentication required (validated via HMAC-SHA256 webhook signature).
Bypass-then-scope pattern (see ``docs/executing/cron-role-and-bypass-then-scope.md``):
a ``cron_session_maker`` bootstrap resolves which tenant the event is for, then
every write happens on an ``async_session_maker`` session with RLS context set
to the owning user — so a bug that mixes up tenants is caught by PostgreSQL,
not just code review.
"""

import hashlib
import hmac
import logging
import uuid as uuid_pkg

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.audit import log_installation_event, log_webhook_received
from app.core.database import async_session_maker, cron_session_maker
from app.core.rls import set_rls_user_context
from app.domain import github_app_installation_ops, github_app_installation_repo_ops

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["webhooks"])


def _verify_signature(body: bytes, signature: str | None) -> bool:
    """Verify GitHub webhook HMAC-SHA256 signature."""
    if not signature or not settings.github_app_webhook_secret:
        return False
    expected = (
        "sha256="
        + hmac.new(
            settings.github_app_webhook_secret.encode(),
            body,
            hashlib.sha256,
        ).hexdigest()
    )
    return hmac.compare_digest(expected, signature)


async def _resolve_installation_owner(
    cron_db: AsyncSession, installation_id: int
) -> tuple[uuid_pkg.UUID, uuid_pkg.UUID] | None:
    """Resolve ``(org_id, owner_user_id)`` for a GitHub installation.

    Single JOIN under ``cron_session_maker`` (BYPASSRLS) — cheaper than an
    ORM fetch and avoids pulling a detachable Organization object across the
    bootstrap/scoped boundary.

    Returns ``None`` when the installation row does not exist yet (e.g. the
    ``installation.created`` event fires before the OAuth ``/link`` callback
    writes the DB row — that case is intentionally a no-op).
    """
    result = await cron_db.execute(
        text(
            "SELECT i.organization_id, o.owner_id "
            "FROM github_app_installations i "
            "JOIN organizations o ON o.id = i.organization_id "
            "WHERE i.installation_id = :iid"
        ),
        {"iid": installation_id},
    )
    row = result.one_or_none()
    if row is None:
        return None
    return row.organization_id, row.owner_id


@router.post("/github")
async def github_webhook(request: Request) -> dict:
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
            await _handle_installation(payload)
        case "installation_repositories":
            await _handle_repo_change(payload)

    return {"ok": True}


async def _handle_installation(payload: dict) -> None:
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

    # ``created`` has no DB row yet — the installation is written by the
    # OAuth ``/link`` callback where we have the org_id from the state param.
    # Short-circuit so we don't bootstrap-lookup a row that won't exist yet.
    if action == "created":
        account = installation_data.get("account", {})
        logger.info(
            f"GitHub App installed: {installation_id} on {account.get('login', 'unknown')}"
        )
        return

    # Bootstrap — resolve owning tenant before opening the scoped session.
    async with cron_session_maker() as cron_db:
        owner = await _resolve_installation_owner(cron_db, installation_id)

    if owner is None:
        logger.warning(f"Installation webhook for unknown installation {installation_id}")
        return
    _, owner_user_id = owner

    async with async_session_maker() as db:
        await set_rls_user_context(db, owner_user_id)
        match action:
            case "deleted":
                log_installation_event("deleted", installation_id)
                await github_app_installation_ops.delete_by_installation_id(db, installation_id)

            case "suspend":
                log_installation_event("suspend", installation_id)
                await github_app_installation_ops.suspend(db, installation_id)

            case "unsuspend":
                log_installation_event("unsuspend", installation_id)
                await github_app_installation_ops.unsuspend(db, installation_id)
        await db.commit()


async def _handle_repo_change(payload: dict) -> None:
    """Handle repos added/removed from an installation."""
    installation_data = payload.get("installation")
    if not installation_data:
        logger.warning("Malformed repo change webhook — missing installation data")
        return
    installation_id = installation_data.get("id")
    if not installation_id:
        logger.warning("Malformed repo change webhook — missing installation id")
        return

    # Bootstrap — resolve (org_id, owner_user_id) and refetch the installation
    # row's PK, which downstream ops use as the FK for installation_repos.
    async with cron_session_maker() as cron_db:
        owner = await _resolve_installation_owner(cron_db, installation_id)
        if owner is None:
            logger.warning(f"Webhook for unknown installation {installation_id} — ignoring")
            return
        installation = await github_app_installation_ops.get_by_installation_id(
            cron_db, installation_id
        )
        if not installation:
            logger.warning(f"Webhook for unknown installation {installation_id} — ignoring")
            return
        installation_db_id = installation.id

    _, owner_user_id = owner

    async with async_session_maker() as db:
        await set_rls_user_context(db, owner_user_id)

        for repo in payload.get("repositories_added", []):
            await github_app_installation_repo_ops.upsert(
                db,
                installation_db_id=installation_db_id,
                github_repo_id=repo["id"],
                full_name=repo["full_name"],
            )
            logger.info(f"Repo added to installation: {repo['full_name']}")

        for repo in payload.get("repositories_removed", []):
            await github_app_installation_repo_ops.delete_by_github_id(
                db,
                installation_db_id=installation_db_id,
                github_repo_id=repo["id"],
            )
            logger.info(f"Repo removed from installation: {repo['full_name']}")

        await db.commit()
