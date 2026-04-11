import uuid as uuid_pkg

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.encryption import token_encryption
from app.models.user_preferences import UserPreferences


class PreferencesOperations:
    """Operations for UserPreferences model."""

    async def get_by_user_id(
        self,
        db: AsyncSession,
        user_id: uuid_pkg.UUID,
    ) -> UserPreferences | None:
        """Get preferences by user ID."""
        statement = select(UserPreferences).where(UserPreferences.user_id == user_id)
        result = await db.execute(statement)
        return result.scalar_one_or_none()

    async def get_or_create(
        self,
        db: AsyncSession,
        user_id: uuid_pkg.UUID,
    ) -> UserPreferences:
        """Get existing preferences or create defaults."""
        prefs = await self.get_by_user_id(db, user_id)
        if prefs:
            return prefs

        # Create default preferences
        prefs = UserPreferences(user_id=user_id)
        db.add(prefs)
        await db.flush()
        await db.refresh(prefs)
        return prefs

    async def update(
        self,
        db: AsyncSession,
        prefs: UserPreferences,
        obj_in: dict,
    ) -> UserPreferences:
        """Update user preferences.

        Encrypts github_token before storing if encryption is enabled.
        """
        for field, value in obj_in.items():
            if hasattr(prefs, field):
                # Encrypt GitHub token before storing
                if field == "github_token" and value:
                    value = token_encryption.encrypt(value)
                setattr(prefs, field, value)
        db.add(prefs)
        await db.flush()
        await db.refresh(prefs)
        return prefs

    def get_decrypted_token(self, prefs: UserPreferences) -> str | None:
        """Get decrypted GitHub token from preferences.

        Args:
            prefs: UserPreferences instance.

        Returns:
            Decrypted token string, or None if not set.
        """
        if not prefs.github_token:
            return None
        return token_encryption.decrypt(prefs.github_token)

    async def clear_github_token(
        self,
        db: AsyncSession,
        user_id: uuid_pkg.UUID,
    ) -> None:
        """Clear a user's stored GitHub token (e.g. after confirmed 401)."""
        prefs = await self.get_by_user_id(db, user_id)
        if prefs and prefs.github_token:
            prefs.github_token = None
            db.add(prefs)
            await db.flush()

    async def validate_github_token(self, token: str) -> dict:
        """
        Validate a GitHub Personal Access Token.

        Returns dict with:
        - 'valid': bool indicating if token is valid
        - 'username': GitHub username if valid
        - 'name': GitHub display name if valid
        - 'scopes': List of granted scopes
        - 'has_repo_scope': True if token can access private repos
        - 'scope_warning': Warning if scopes are insufficient
        - 'error': Error message if invalid
        """
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    "https://api.github.com/user",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/vnd.github.v3+json",
                    },
                    timeout=10.0,
                )

                if response.status_code == 200:
                    data = response.json()

                    # Parse scopes from X-OAuth-Scopes header
                    scopes_header = response.headers.get("X-OAuth-Scopes", "")
                    scopes = [s.strip() for s in scopes_header.split(",") if s.strip()]

                    # Check if token has repo scope for private repos
                    has_repo_scope = "repo" in scopes

                    result: dict = {
                        "valid": True,
                        "username": data.get("login"),
                        "name": data.get("name"),
                        "scopes": scopes,
                        "has_repo_scope": has_repo_scope,
                    }

                    # Add warning if only public_repo scope
                    if not has_repo_scope:
                        if "public_repo" in scopes:
                            result["scope_warning"] = (
                                "Token only has 'public_repo' scope. "
                                "Private repositories will not be accessible."
                            )
                        elif not scopes:
                            # Fine-grained PAT or no scopes - may still work
                            result["scope_warning"] = (
                                "Token scopes could not be determined. "
                                "If you have issues with private repos, "
                                "ensure your token has repository access."
                            )

                    return result
                elif response.status_code == 401:
                    return {"valid": False, "error": "Invalid or expired token"}
                else:
                    return {"valid": False, "error": f"GitHub API error: {response.status_code}"}
        except httpx.TimeoutException:
            return {"valid": False, "error": "GitHub API timeout"}
        except Exception as e:
            return {"valid": False, "error": str(e)}


preferences_ops = PreferencesOperations()
