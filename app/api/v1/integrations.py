"""GitHub App installation management endpoints.

Provides endpoints for:
- Linking a GitHub App installation to a Trajan organization (after OAuth callback)
- Querying installation status for an org
- Removing an installation record
"""

import logging
import uuid as uuid_pkg

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.api.deps import CurrentUser, DbSession
from app.domain import github_app_installation_ops
from app.domain.organization_operations import organization_ops
from app.models.organization import MemberRole
from app.services.github.app_auth import github_app_auth
from app.services.github.http_client import get_github_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/integrations", tags=["integrations"])


class LinkInstallationRequest(BaseModel):
    installation_id: int
    organization_id: uuid_pkg.UUID


class GitHubAppInstallationResponse(BaseModel):
    id: str
    installation_id: int
    organization_id: str
    github_account_login: str
    github_account_type: str
    repository_selection: str
    suspended_at: str | None
    created_at: str


@router.post("/github-app/link", response_model=GitHubAppInstallationResponse)
async def link_github_installation(
    data: LinkInstallationRequest,
    db: DbSession,
    current_user: CurrentUser,
) -> GitHubAppInstallationResponse:
    """Link a GitHub App installation to a Trajan organization."""
    # Require org admin to link installations
    role = await organization_ops.get_member_role(db, data.organization_id, current_user.id)
    if role not in (MemberRole.OWNER.value, MemberRole.ADMIN.value):
        raise HTTPException(403, "Admin or owner access required")

    if not github_app_auth.is_configured:
        raise HTTPException(400, "GitHub App is not configured")

    # Check if org already has an installation
    existing = await github_app_installation_ops.get_for_org(db, data.organization_id)
    if existing:
        raise HTTPException(
            409, "Organization already has a GitHub App installation linked"
        )

    # Verify the installation exists on GitHub
    try:
        app_jwt = github_app_auth.create_app_jwt()
        client = get_github_client()
        resp = await client.get(
            f"https://api.github.com/app/installations/{data.installation_id}",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
            },
        )
    except Exception:
        logger.exception("Failed to verify installation on GitHub")
        raise HTTPException(502, "Could not verify installation with GitHub") from None
    if resp.status_code != 200:
        raise HTTPException(400, "Installation not found on GitHub")
    gh_data = resp.json()

    # Create DB record
    installation = await github_app_installation_ops.create_installation(
        db,
        obj_in={
            "installation_id": data.installation_id,
            "organization_id": data.organization_id,
            "github_account_login": gh_data["account"]["login"],
            "github_account_type": gh_data["account"]["type"],
            "installed_by_user_id": current_user.id,
            "permissions": gh_data.get("permissions", {}),
            "repository_selection": gh_data.get("repository_selection", "all"),
        },
    )

    logger.info(
        f"Linked GitHub App installation {data.installation_id} "
        f"to org {data.organization_id} by user {current_user.id}"
    )

    return GitHubAppInstallationResponse(
        id=str(installation.id),
        installation_id=installation.installation_id,
        organization_id=str(installation.organization_id),
        github_account_login=installation.github_account_login,
        github_account_type=installation.github_account_type,
        repository_selection=installation.repository_selection,
        suspended_at=installation.suspended_at.isoformat() if installation.suspended_at else None,
        created_at=installation.created_at.isoformat(),
    )


@router.get("/github-app/{organization_id}")
async def get_github_installation(
    organization_id: uuid_pkg.UUID,
    db: DbSession,
    current_user: CurrentUser,
) -> GitHubAppInstallationResponse | None:
    """Get the GitHub App installation for an organization."""
    # Require org membership to view installation status
    is_member = await organization_ops.is_member(db, organization_id, current_user.id)
    if not is_member:
        raise HTTPException(403, "Not a member of this organization")

    installation = await github_app_installation_ops.get_for_org(db, organization_id)
    if not installation:
        return None

    return GitHubAppInstallationResponse(
        id=str(installation.id),
        installation_id=installation.installation_id,
        organization_id=str(installation.organization_id),
        github_account_login=installation.github_account_login,
        github_account_type=installation.github_account_type,
        repository_selection=installation.repository_selection,
        suspended_at=installation.suspended_at.isoformat() if installation.suspended_at else None,
        created_at=installation.created_at.isoformat(),
    )


@router.delete("/github-app/{organization_id}")
async def remove_github_installation(
    organization_id: uuid_pkg.UUID,
    db: DbSession,
    current_user: CurrentUser,
) -> dict:
    """Remove a GitHub App installation record (does not uninstall from GitHub)."""
    # Require org admin to remove installations
    role = await organization_ops.get_member_role(db, organization_id, current_user.id)
    if role not in (MemberRole.OWNER.value, MemberRole.ADMIN.value):
        raise HTTPException(403, "Admin or owner access required")

    installation = await github_app_installation_ops.get_for_org(db, organization_id)
    if installation:
        await github_app_installation_ops.delete_installation(db, installation.id)
        logger.info(
            f"Removed GitHub App installation for org {organization_id} by user {current_user.id}"
        )
    return {"ok": True}
