"""GitHub App authentication service.

Handles JWT signing and installation access token generation.
Installation tokens are cached for 55 minutes (they last 60 minutes from GitHub).

Uses python-jose (already a project dependency) for RS256 JWT signing.
"""

import logging
import time
from datetime import UTC, datetime, timedelta

from cachetools import TTLCache

from app.config import settings
from app.services.github.http_client import get_github_client

logger = logging.getLogger(__name__)


class GitHubAppAuth:
    def __init__(self) -> None:
        # Cache: installation_id → (token, expires_at)
        # TTL 55 min — tokens last 60 min, we refresh 5 min early
        self._token_cache: TTLCache[int, tuple[str, datetime]] = TTLCache(
            maxsize=100, ttl=3300
        )

    def create_app_jwt(self) -> str:
        """Create a short-lived JWT signed with the App's private key."""
        from jose import jwt as jose_jwt

        now = int(time.time())
        payload = {
            "iat": now - 60,  # 60s in the past (clock skew tolerance)
            "exp": now + (10 * 60),  # 10 minutes
            "iss": settings.github_app_id,
        }
        # Handle newline escaping in PEM key from env vars
        private_key = settings.github_app_private_key.replace("\\n", "\n")
        return jose_jwt.encode(payload, private_key, algorithm="RS256")

    async def get_installation_token(self, installation_id: int) -> str:
        """Get a valid installation access token.

        Returns cached token if still valid, otherwise generates a new one.
        """
        cached = self._token_cache.get(installation_id)
        if cached is not None:
            token, expires_at = cached
            if datetime.now(UTC) < expires_at - timedelta(minutes=5):
                return token

        app_jwt = self.create_app_jwt()
        client = get_github_client()
        response = await client.post(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
            },
        )
        response.raise_for_status()
        data = response.json()

        token = data["token"]
        expires_at = datetime.fromisoformat(
            data["expires_at"].replace("Z", "+00:00")
        )
        self._token_cache[installation_id] = (token, expires_at)
        logger.info(f"Generated new installation token for {installation_id}")
        return token

    @property
    def is_configured(self) -> bool:
        """Check if GitHub App credentials are configured."""
        return bool(settings.github_app_id and settings.github_app_private_key)


# Singleton
github_app_auth = GitHubAppAuth()
