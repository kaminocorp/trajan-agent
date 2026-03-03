from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_with_rls
from app.domain.org_member_operations import org_member_ops
from app.domain.organization_operations import organization_ops
from app.domain.user_operations import user_ops
from app.models.subscription import PlanTier
from app.models.user import User

router = APIRouter(prefix="/users", tags=["users"])


class UserRead(BaseModel):
    """User profile response."""

    id: str
    email: str | None
    display_name: str | None
    avatar_url: str | None
    github_username: str | None
    auth_provider: str | None
    created_at: str
    onboarding_completed_at: str | None

    class Config:
        from_attributes = True


class UserUpdate(BaseModel):
    """User profile update request."""

    display_name: str | None = None
    avatar_url: str | None = None


class InvitedOrgInfo(BaseModel):
    """Info about an organization the user was invited to."""

    id: str
    name: str
    slug: str
    inviter_name: str | None  # Display name of person who invited
    inviter_email: str | None  # Fallback if no display name
    invited_at: str | None


# --- Deletion Preview Schemas ---


class OrgMemberInfoResponse(BaseModel):
    """Info about an organization member (for ownership transfer selection)."""

    id: str
    email: str | None
    name: str | None
    role: str


class OrgDeletionPreviewResponse(BaseModel):
    """Preview of what happens to an owned organization during account deletion."""

    org_id: str
    org_name: str
    is_sole_member: bool
    member_count: int
    other_members: list[OrgMemberInfoResponse]
    product_count: int
    work_item_count: int
    document_count: int
    has_active_subscription: bool


class BasicOrgInfoResponse(BaseModel):
    """Minimal info about an org where user is just a member."""

    id: str
    name: str


class DeletionPreviewResponse(BaseModel):
    """Complete deletion preview - shows what will happen if user deletes their account."""

    owned_orgs: list[OrgDeletionPreviewResponse]
    member_only_orgs: list[BasicOrgInfoResponse]
    total_products_affected: int
    total_work_items_affected: int
    total_documents_affected: int


class AccountDeletionRequest(BaseModel):
    """Request to delete user account with explicit org handling."""

    orgs_to_delete: list[str] = []  # UUIDs of orgs where user is sole member, choosing to delete
    confirm_deletion: bool = False  # Must be True to proceed

    @classmethod
    def validate_confirmation(cls, v: bool) -> bool:
        """Ensure deletion is explicitly confirmed."""
        if not v:
            raise ValueError("Must confirm deletion by setting confirm_deletion to true")
        return v


class CreateWorkspaceRequest(BaseModel):
    """Request to create a personal workspace during onboarding."""

    name: str


class CreateWorkspaceResponse(BaseModel):
    """Response after creating a personal workspace."""

    id: str
    name: str
    slug: str


class CheckTeamSlugRequest(BaseModel):
    """Request to check an org slug during onboarding."""

    slug: str


class CheckTeamSlugResponse(BaseModel):
    """Response for slug check — privacy-safe."""

    exists: bool
    is_member: bool
    org_id: str | None = None  # Only populated if is_member=True
    org_name: str | None = None  # Only populated if is_member=True


class OnboardingContext(BaseModel):
    """Context for frontend to determine which onboarding flow to show."""

    # Orgs user was invited to (not owner)
    invited_orgs: list[InvitedOrgInfo]

    # Does the user own any organization?
    has_personal_org: bool

    # User's personal org needs setup? (owner + plan_tier = 'none')
    personal_org_incomplete: bool
    personal_org_id: str | None
    personal_org_name: str | None

    # Has user completed onboarding before?
    onboarding_completed: bool

    # Recommended flow: "full" | "invited" | "returning"
    recommended_flow: Literal["full", "invited", "returning"]


def user_to_response(user: User) -> dict:
    """Convert User model to response dict."""
    return {
        "id": str(user.id),
        "email": user.email,
        "display_name": user.display_name,
        "avatar_url": user.avatar_url,
        "github_username": user.github_username,
        "auth_provider": user.auth_provider,
        "created_at": user.created_at.isoformat(),
        "onboarding_completed_at": (
            user.onboarding_completed_at.isoformat() if user.onboarding_completed_at else None
        ),
    }


@router.get("/me", response_model=UserRead)
async def get_current_user_profile(
    current_user: User = Depends(get_current_user),
):
    """Get the current user's profile."""
    return user_to_response(current_user)


@router.patch("/me", response_model=UserRead)
async def update_current_user_profile(
    data: UserUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """Update the current user's profile."""
    update_data = data.model_dump(exclude_unset=True)

    if not update_data:
        return user_to_response(current_user)

    updated_user = await user_ops.update(db, current_user, update_data)
    return user_to_response(updated_user)


@router.get("/me/deletion-preview", response_model=DeletionPreviewResponse)
async def get_deletion_preview(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """
    Get a preview of what will happen when the user deletes their account.

    Returns detailed information about:
    - Organizations the user owns (with member counts and whether transfer is required)
    - Organizations where the user is just a member (membership will be removed)
    - Total counts of products, work items, and documents that will be deleted

    For owned organizations:
    - If sole member: organization and all data will be deleted
    - If has other members: must transfer ownership before deletion
    """
    preview = await user_ops.get_deletion_preview(db, current_user.id)

    # Convert dataclass results to response format
    return DeletionPreviewResponse(
        owned_orgs=[
            OrgDeletionPreviewResponse(
                org_id=str(org.org_id),
                org_name=org.org_name,
                is_sole_member=org.is_sole_member,
                member_count=org.member_count,
                other_members=[
                    OrgMemberInfoResponse(
                        id=str(m.id),
                        email=m.email,
                        name=m.name,
                        role=m.role,
                    )
                    for m in org.other_members
                ],
                product_count=org.product_count,
                work_item_count=org.work_item_count,
                document_count=org.document_count,
                has_active_subscription=org.has_active_subscription,
            )
            for org in preview.owned_orgs
        ],
        member_only_orgs=[
            BasicOrgInfoResponse(id=str(org.id), name=org.name) for org in preview.member_only_orgs
        ],
        total_products_affected=preview.total_products_affected,
        total_work_items_affected=preview.total_work_items_affected,
        total_documents_affected=preview.total_documents_affected,
    )


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
async def delete_current_user(
    request: AccountDeletionRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """
    Delete the current user's account and all associated data.

    This action is irreversible and requires explicit handling for owned organizations:

    - **orgs_to_delete**: List of org IDs where user is sole member. These orgs
      and all their data (products, work items, documents) will be permanently deleted.
    - **confirm_deletion**: Must be `true` to proceed.

    **Validation rules:**
    - All owned orgs with other members must be transferred first (via /transfer-ownership)
    - All sole-member orgs must be explicitly listed in orgs_to_delete
    - Memberships in other orgs (where user is not owner) are removed automatically

    **Cascade behavior:**
    - Sole-member orgs → deleted with all products, work items, documents
    - User preferences → deleted
    - Organization memberships → deleted
    - Product access entries → deleted
    """
    # Validate confirmation
    if not request.confirm_deletion:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Must confirm deletion by setting confirm_deletion to true",
        )

    # Parse org IDs
    import uuid as uuid_pkg

    try:
        orgs_to_delete = [uuid_pkg.UUID(org_id) for org_id in request.orgs_to_delete]
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid org_id format in orgs_to_delete",
        ) from e

    # Perform deletion with validation
    try:
        await user_ops.delete_with_cascade(db, current_user.id, orgs_to_delete)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e),
        ) from e

    await db.commit()


@router.post("/me/complete-onboarding", response_model=UserRead)
async def complete_onboarding(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """Mark the current user's onboarding as complete."""
    if current_user.onboarding_completed_at is not None:
        # Already completed, just return current state
        return user_to_response(current_user)

    updated_user = await user_ops.update(
        db, current_user, {"onboarding_completed_at": datetime.now(UTC)}
    )
    return user_to_response(updated_user)


@router.post("/me/create-workspace", response_model=CreateWorkspaceResponse)
async def create_workspace(
    data: CreateWorkspaceRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """
    Create a personal workspace during onboarding.

    Guards against duplicates — returns 409 if user already owns an organization.
    """
    # Check if user already owns an org
    existing_orgs = await organization_ops.get_for_user(db, current_user.id)
    owned = [org for org in existing_orgs if org.owner_id == current_user.id]
    if owned:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User already owns a workspace",
        )

    org = await organization_ops.create(
        db,
        name=data.name,
        owner_id=current_user.id,
    )

    return CreateWorkspaceResponse(
        id=str(org.id),
        name=org.name,
        slug=org.slug,
    )


@router.post("/me/check-team-slug", response_model=CheckTeamSlugResponse)
async def check_team_slug(
    data: CheckTeamSlugRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """
    Check if an org slug exists and whether the current user is a member.

    Privacy-safe: only reveals org name/ID to existing members.
    Used during onboarding "Join a team" flow.
    """
    slug = data.slug.strip().lower()
    org = await organization_ops.get_by_slug(db, slug)

    if not org:
        return CheckTeamSlugResponse(exists=False, is_member=False)

    role = await organization_ops.get_member_role(db, org.id, current_user.id)

    if role is not None:
        return CheckTeamSlugResponse(
            exists=True,
            is_member=True,
            org_id=str(org.id),
            org_name=org.name,
        )

    return CheckTeamSlugResponse(exists=True, is_member=False)


@router.get("/me/onboarding-context", response_model=OnboardingContext)
async def get_onboarding_context(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
    has_referral: bool = False,
):
    """
    Get context to determine which onboarding flow to show.

    Returns information about invited organizations and personal org status
    to help the frontend decide between full onboarding, invited user flow,
    or redirect for returning users.

    Args:
        has_referral: If True, indicates the user arrived via a referral link.
                     Referral users are always routed to "full" onboarding to
                     ensure they select a plan and properly redeem their reward.
    """
    # Get all memberships with org, subscription, and inviter info
    memberships_with_inviters = await org_member_ops.get_by_user_with_details(db, current_user.id)

    invited_orgs: list[InvitedOrgInfo] = []
    has_personal_org = False
    personal_org_incomplete = False
    personal_org_id: str | None = None
    personal_org_name: str | None = None

    for membership, inviter in memberships_with_inviters:
        org = membership.organization
        if not org:
            continue

        subscription = org.subscription

        if org.owner_id == current_user.id:
            has_personal_org = True
            # User owns this org - check if setup is incomplete
            plan_tier = subscription.plan_tier if subscription else PlanTier.NONE.value
            if plan_tier == PlanTier.NONE.value:
                personal_org_incomplete = True
                personal_org_id = str(org.id)
                personal_org_name = org.name
        else:
            # User was invited to this org
            invited_orgs.append(
                InvitedOrgInfo(
                    id=str(org.id),
                    name=org.name,
                    slug=org.slug,
                    inviter_name=inviter.display_name if inviter else None,
                    inviter_email=inviter.email if inviter else None,
                    invited_at=(
                        membership.invited_at.isoformat() if membership.invited_at else None
                    ),
                )
            )

    # Determine recommended flow
    onboarding_completed = current_user.onboarding_completed_at is not None

    if onboarding_completed:
        recommended_flow: Literal["full", "invited", "returning"] = "returning"
    elif has_referral:
        # Referral users MUST go through full onboarding to select a plan
        # and properly redeem their referral reward (free month)
        recommended_flow = "full"
    elif invited_orgs and not has_personal_org:
        # Has invites AND no personal org = invited user flow
        recommended_flow = "invited"
    else:
        # Direct signup, or has personal org needing plan setup
        recommended_flow = "full"

    return OnboardingContext(
        invited_orgs=invited_orgs,
        has_personal_org=has_personal_org,
        personal_org_incomplete=personal_org_incomplete,
        personal_org_id=personal_org_id,
        personal_org_name=personal_org_name,
        onboarding_completed=onboarding_completed,
        recommended_flow=recommended_flow,
    )
