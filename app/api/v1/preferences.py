from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_with_rls
from app.domain.preferences_operations import preferences_ops
from app.models.user import User
from app.models.user_preferences import UserPreferences

router = APIRouter(prefix="/users/me/preferences", tags=["preferences"])


class PreferencesRead(BaseModel):
    """User preferences response.

    Digest settings (email_digest, digest_product_ids, etc.) have moved to
    per-org preferences at GET /organizations/{org_id}/digest-preferences.
    """

    notify_work_items: bool
    notify_documents: bool
    github_token_set: bool  # Don't expose actual token
    default_view: str
    sidebar_default: str
    auto_generate_docs: bool
    github_setup_dismissed: bool
    github_connect_modal_dismissed: bool
    invite_box_dismissed: bool
    getting_started_dismissed: bool

    class Config:
        from_attributes = True


class PreferencesUpdate(BaseModel):
    """User preferences update request."""

    notify_work_items: bool | None = None
    notify_documents: bool | None = None
    github_token: str | None = None
    default_view: str | None = None
    sidebar_default: str | None = None
    auto_generate_docs: bool | None = None
    github_setup_dismissed: bool | None = None
    github_connect_modal_dismissed: bool | None = None
    invite_box_dismissed: bool | None = None
    getting_started_dismissed: bool | None = None


class GitHubTokenTest(BaseModel):
    """GitHub token validation request."""

    token: str


class GitHubTokenTestResult(BaseModel):
    """GitHub token validation response."""

    valid: bool
    username: str | None = None
    name: str | None = None
    scopes: list[str] | None = None
    has_repo_scope: bool | None = None
    scope_warning: str | None = None
    error: str | None = None


def prefs_to_response(prefs: UserPreferences) -> dict:
    """Convert UserPreferences model to response dict."""
    return {
        "notify_work_items": prefs.notify_work_items,
        "notify_documents": prefs.notify_documents,
        "github_token_set": prefs.github_token is not None and len(prefs.github_token) > 0,
        "default_view": prefs.default_view,
        "sidebar_default": prefs.sidebar_default,
        "auto_generate_docs": prefs.auto_generate_docs,
        "github_setup_dismissed": prefs.github_setup_dismissed,
        "github_connect_modal_dismissed": prefs.github_connect_modal_dismissed,
        "invite_box_dismissed": prefs.invite_box_dismissed,
        "getting_started_dismissed": prefs.getting_started_dismissed,
    }


@router.get("", response_model=PreferencesRead)
async def get_preferences(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """
    Get the current user's preferences.

    Creates default preferences if none exist.
    """
    prefs = await preferences_ops.get_or_create(db, current_user.id)
    return prefs_to_response(prefs)


@router.patch("", response_model=PreferencesRead)
async def update_preferences(
    data: PreferencesUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """Update the current user's preferences."""
    prefs = await preferences_ops.get_or_create(db, current_user.id)

    update_data = data.model_dump(exclude_unset=True)

    # Validate enum values
    if "default_view" in update_data and update_data["default_view"] not in ("grid", "list"):
        update_data["default_view"] = "grid"

    if "sidebar_default" in update_data and update_data["sidebar_default"] not in (
        "expanded",
        "collapsed",
    ):
        update_data["sidebar_default"] = "expanded"

    # Handle empty token as removal
    if "github_token" in update_data and update_data["github_token"] == "":
        update_data["github_token"] = None

    if update_data:
        prefs = await preferences_ops.update(db, prefs, update_data)

    return prefs_to_response(prefs)


@router.post("/test-github-token", response_model=GitHubTokenTestResult)
async def test_github_token(
    data: GitHubTokenTest,
    _current_user: User = Depends(get_current_user),
):
    """
    Test a GitHub Personal Access Token.

    Returns validation status and GitHub username if valid.
    Does not save the token.
    """
    result = await preferences_ops.validate_github_token(data.token)
    return result
