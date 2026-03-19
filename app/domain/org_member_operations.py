"""Domain operations for OrganizationMember model."""

import asyncio
import logging
import re
import uuid as uuid_pkg
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config.settings import settings
from app.models.organization import MemberRole, OrganizationMember
from app.models.user import User
from app.services.email.postmark import postmark_service
from app.services.supabase import get_supabase_admin_client

logger = logging.getLogger(__name__)

# Simple email validation pattern (RFC 5322 simplified)
EMAIL_PATTERN = re.compile(r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$")


class InvalidEmailError(Exception):
    """Raised when email format is invalid."""

    pass


class SupabaseInviteError(Exception):
    """Raised when Supabase invite fails."""

    pass


class OrgMemberOperations:
    """CRUD operations for OrganizationMember model."""

    async def get(
        self,
        db: AsyncSession,
        id: uuid_pkg.UUID,
    ) -> OrganizationMember | None:
        """Get a membership by ID."""
        statement = select(OrganizationMember).where(OrganizationMember.id == id)
        result = await db.execute(statement)
        return result.scalar_one_or_none()

    async def get_by_org_and_user(
        self,
        db: AsyncSession,
        organization_id: uuid_pkg.UUID,
        user_id: uuid_pkg.UUID,
    ) -> OrganizationMember | None:
        """Get a specific membership by org and user."""
        statement = select(OrganizationMember).where(
            OrganizationMember.organization_id == organization_id,
            OrganizationMember.user_id == user_id,
        )
        result = await db.execute(statement)
        return result.scalar_one_or_none()

    async def get_by_org(
        self,
        db: AsyncSession,
        organization_id: uuid_pkg.UUID,
    ) -> list[OrganizationMember]:
        """Get all members of an organization."""
        statement = (
            select(OrganizationMember)
            .where(OrganizationMember.organization_id == organization_id)
            .options(selectinload(OrganizationMember.user))
            .order_by(OrganizationMember.joined_at.desc())
        )
        result = await db.execute(statement)
        return list(result.scalars().all())

    async def get_by_user(
        self,
        db: AsyncSession,
        user_id: uuid_pkg.UUID,
    ) -> list[OrganizationMember]:
        """Get all memberships for a user."""
        statement = (
            select(OrganizationMember)
            .where(OrganizationMember.user_id == user_id)
            .options(selectinload(OrganizationMember.organization))
            .order_by(OrganizationMember.joined_at.desc())
        )
        result = await db.execute(statement)
        return list(result.scalars().all())

    async def get_by_user_with_details(
        self,
        db: AsyncSession,
        user_id: uuid_pkg.UUID,
    ) -> list[tuple[OrganizationMember, User | None]]:
        """
        Get all memberships for a user with organization, subscription, and inviter info.

        Returns tuples of (membership, inviter_user) where inviter_user is the User
        who invited this member (or None if no inviter).
        """
        from sqlalchemy.orm import aliased

        from app.models.organization import Organization
        from app.models.subscription import Subscription

        # Create an alias for the inviter user to avoid ambiguity
        InviterUser = aliased(User)

        statement = (
            select(OrganizationMember, InviterUser)
            .where(OrganizationMember.user_id == user_id)
            .join(Organization, OrganizationMember.organization_id == Organization.id)
            .outerjoin(Subscription, Subscription.organization_id == Organization.id)
            .outerjoin(InviterUser, OrganizationMember.invited_by == InviterUser.id)
            .options(
                selectinload(OrganizationMember.organization).selectinload(
                    Organization.subscription
                )
            )
            .order_by(OrganizationMember.joined_at.desc())
        )
        result = await db.execute(statement)
        return list(result.all())

    async def count_by_org(
        self,
        db: AsyncSession,
        organization_id: uuid_pkg.UUID,
    ) -> int:
        """Count members in an organization."""
        statement = (
            select(func.count())
            .select_from(OrganizationMember)
            .where(OrganizationMember.organization_id == organization_id)
        )
        result = await db.execute(statement)
        return result.scalar() or 0

    async def add_member(
        self,
        db: AsyncSession,
        organization_id: uuid_pkg.UUID,
        user_id: uuid_pkg.UUID,
        role: str = MemberRole.MEMBER.value,
        invited_by: uuid_pkg.UUID | None = None,
    ) -> OrganizationMember:
        """Add a user to an organization."""
        member = OrganizationMember(
            organization_id=organization_id,
            user_id=user_id,
            role=role,
            invited_by=invited_by,
            invited_at=datetime.now(UTC) if invited_by else None,
        )
        db.add(member)
        await db.flush()
        await db.refresh(member)
        return member

    async def update_role(
        self,
        db: AsyncSession,
        membership: OrganizationMember,
        new_role: MemberRole,
    ) -> OrganizationMember:
        """Update a member's role."""
        membership.role = new_role
        db.add(membership)
        await db.flush()
        await db.refresh(membership)
        return membership

    async def remove_member(
        self,
        db: AsyncSession,
        organization_id: uuid_pkg.UUID,
        user_id: uuid_pkg.UUID,
    ) -> bool:
        """Remove a user from an organization."""
        member = await self.get_by_org_and_user(db, organization_id, user_id)
        if member:
            await db.delete(member)
            await db.flush()
            return True
        return False

    async def find_user_by_email(
        self,
        db: AsyncSession,
        email: str,
    ) -> User | None:
        """Find a user by email for invitations (case-insensitive)."""
        statement = select(User).where(func.lower(User.email) == email.lower())
        result = await db.execute(statement)
        return result.scalar_one_or_none()

    async def create_user_via_supabase(
        self,
        db: AsyncSession,
        email: str,
        inviter_name: str | None = None,
        org_name: str | None = None,
    ) -> User:
        """
        Create a user via Supabase Admin API.

        This invites the user by email - Supabase creates the auth.users record
        and sends an invite email automatically. The user clicks the link,
        sets their password, and their account is activated.

        Returns the newly created public.users record.

        Raises:
            InvalidEmailError: If email format is invalid.
            SupabaseInviteError: If Supabase API call fails.
        """
        # Validate email format before calling external API
        if not EMAIL_PATTERN.match(email):
            raise InvalidEmailError(f"Invalid email format: {email}")

        try:
            supabase = get_supabase_admin_client()
            # Build redirect URL to our auth callback (not Supabase's default)
            redirect_url = f"{settings.frontend_url}/auth/callback"
            invite_options = {"redirect_to": redirect_url}

            # Use thread pool to avoid blocking the async event loop
            response = await asyncio.to_thread(
                supabase.auth.admin.invite_user_by_email, email, invite_options
            )
            supabase_user_id = uuid_pkg.UUID(response.user.id)
        except Exception as e:
            error_msg = str(e).lower()

            # Handle race condition: user already exists in Supabase auth
            if "already been registered" in error_msg or "already exists" in error_msg:
                logger.info(f"User {email} already exists in Supabase, syncing to local DB")

                # First check our local database
                resolved_user = await self.find_user_by_email(db, email)

                # User exists in Supabase but not locally - fetch and sync
                if not resolved_user:
                    try:
                        resolved_user = await self._sync_user_from_supabase(db, email)
                    except Exception as sync_error:
                        logger.error(f"Failed to sync Supabase user {email}: {sync_error}")

                if not resolved_user:
                    raise SupabaseInviteError(
                        "User exists in auth system but sync failed. Please try again."
                    ) from e

                # Send invite email via magic link (Supabase invite can't be used
                # for existing users, so we generate a magic link and email it)
                try:
                    await self._send_invite_to_existing_user(
                        email=email,
                        inviter_name=inviter_name,
                        org_name=org_name,
                    )
                except SupabaseInviteError:
                    logger.warning(
                        f"Could not send invite email to existing user {email} "
                        f"(member record will still be created)"
                    )

                return resolved_user

            # Log and re-raise other errors with cleaner message
            logger.error(f"Supabase invite failed for {email}: {e}")
            raise SupabaseInviteError("Failed to send invite email. Please try again later.") from e

        # Return existing public.users record if present (e.g. Supabase
        # reactivated a soft-deleted auth user with the same UUID)
        existing = await db.get(User, supabase_user_id)
        if existing:
            return existing

        # Create public.users record
        user = User(id=supabase_user_id, email=email)
        db.add(user)
        await db.flush()
        await db.refresh(user)
        return user

    async def _sync_user_from_supabase(
        self,
        db: AsyncSession,
        email: str,
    ) -> User | None:
        """
        Fetch a user from Supabase admin API and create local record.

        Used for recovery when user exists in Supabase auth but not in public.users.
        """
        supabase = get_supabase_admin_client()

        # Fetch users from Supabase (runs in thread pool)
        response = await asyncio.to_thread(supabase.auth.admin.list_users)

        # Find user by email
        supabase_user = next((u for u in response if u.email == email), None)
        if not supabase_user:
            return None

        # Check if user already exists locally by ID (email case mismatch)
        supabase_id = uuid_pkg.UUID(supabase_user.id)
        existing = await db.get(User, supabase_id)
        if existing:
            logger.info(f"User {email} already exists locally (id={supabase_id})")
            return existing

        # Create local record
        user = User(id=supabase_id, email=email)
        db.add(user)
        await db.flush()
        await db.refresh(user)
        logger.info(f"Synced Supabase user {email} to local database")
        return user

    async def _send_invite_to_existing_user(
        self,
        email: str,
        inviter_name: str | None,
        org_name: str | None,
    ) -> bool:
        """Generate a magic link and send a team invite email for an existing Supabase user.

        Used as a fallback when `invite_user_by_email` fails because the user
        already has a Supabase auth account.

        Returns True if the email was sent, False if Postmark delivery failed
        (non-fatal — the member record is still created by the caller).

        Raises:
            SupabaseInviteError: If `generate_link` itself fails.
        """
        try:
            supabase = get_supabase_admin_client()
            redirect_url = f"{settings.frontend_url}/auth/callback"
            response = await asyncio.to_thread(
                supabase.auth.admin.generate_link,
                {
                    "type": "magiclink",
                    "email": email,
                    "options": {"redirect_to": redirect_url},
                },
            )
            magic_link = response.properties.action_link
        except Exception as e:
            logger.error(f"Failed to generate magic link for {email}: {e}")
            raise SupabaseInviteError(
                "Failed to generate login link for existing user. Please try again later."
            ) from e

        sent = await postmark_service.send_team_invite(
            to=email,
            inviter_name=inviter_name,
            org_name=org_name,
            magic_link=magic_link,
        )
        if not sent:
            logger.warning(
                f"Postmark delivery failed for team invite to {email} "
                f"(member record was still created)"
            )
        return sent

    async def resend_invite(
        self,
        email: str,
        inviter_name: str | None = None,
        org_name: str | None = None,
    ) -> None:
        """
        Resend invite email to a pending user via Supabase Admin API.

        Calling invite_user_by_email on an existing user resends the invite
        with a fresh link. If the user already has a confirmed Supabase account,
        falls back to generating a magic link and sending a team invite email.

        Raises:
            SupabaseInviteError: If both invite and magic link fallback fail.
        """
        try:
            supabase = get_supabase_admin_client()
            redirect_url = f"{settings.frontend_url}/auth/callback"
            invite_options = {"redirect_to": redirect_url}

            await asyncio.to_thread(supabase.auth.admin.invite_user_by_email, email, invite_options)
            logger.info(f"Resent invite email to {email}")
        except Exception as e:
            error_msg = str(e).lower()

            if "already been registered" in error_msg or "already exists" in error_msg:
                logger.info(
                    f"User {email} already registered in Supabase, "
                    f"resending via magic link fallback"
                )
                await self._send_invite_to_existing_user(
                    email=email,
                    inviter_name=inviter_name,
                    org_name=org_name,
                )
                return

            logger.error(f"Failed to resend invite to {email}: {e}")
            raise SupabaseInviteError(
                "Failed to resend invite email. Please try again later."
            ) from e

    async def get_members_with_tokens(
        self,
        db: AsyncSession,
        organization_id: uuid_pkg.UUID,
    ) -> list[OrganizationMember]:
        """Get org owners/admins who have a GitHub token configured.

        Returns members ordered by role priority (owners first, then admins)
        so callers can prefer the highest-privilege token.
        """
        from sqlalchemy import case

        from app.models.user_preferences import UserPreferences

        role_priority = case(
            (OrganizationMember.role == MemberRole.OWNER.value, 0),
            else_=1,
        )
        statement = (
            select(OrganizationMember)
            .join(
                UserPreferences,
                UserPreferences.user_id == OrganizationMember.user_id,
            )
            .where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.role.in_([MemberRole.OWNER.value, MemberRole.ADMIN.value]),
                UserPreferences.github_token.is_not(None),  # type: ignore[union-attr]
            )
            .order_by(role_priority)
        )
        result = await db.execute(statement)
        return list(result.scalars().all())

    async def get_owners(
        self,
        db: AsyncSession,
        organization_id: uuid_pkg.UUID,
    ) -> list[OrganizationMember]:
        """Get all owners of an organization."""
        statement = (
            select(OrganizationMember)
            .where(
                OrganizationMember.organization_id == organization_id,
                OrganizationMember.role == MemberRole.OWNER.value,
            )
            .options(selectinload(OrganizationMember.user))
        )
        result = await db.execute(statement)
        return list(result.scalars().all())

    async def is_only_owner(
        self,
        db: AsyncSession,
        organization_id: uuid_pkg.UUID,
        user_id: uuid_pkg.UUID,
    ) -> bool:
        """Check if a user is the only owner of an organization."""
        owners = await self.get_owners(db, organization_id)
        return len(owners) == 1 and owners[0].user_id == user_id


org_member_ops = OrgMemberOperations()
