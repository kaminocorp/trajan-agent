"""JWT validation and user authentication dependencies.

This module provides:
- JWT validation against Supabase JWKS
- User authentication and auto-creation
- RLS-aware database session dependency
"""

import logging
import time
import uuid as uuid_pkg
from collections.abc import AsyncGenerator
from typing import Annotated, Any

import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from jose.backends import ECKey
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.database import get_db
from app.core.rls import set_rls_user_context
from app.models.user import User

logger = logging.getLogger(__name__)

security = HTTPBearer(auto_error=False)

# Cache for JWKS with TTL to handle key rotation
_jwks_cache: dict[str, Any] = {}
_jwks_cache_timestamp: float = 0.0
_JWKS_CACHE_TTL_SECONDS: float = 3600.0  # 1 hour


async def _fetch_jwks() -> dict[str, Any]:
    """Fetch JWKS from Supabase and update the cache."""
    global _jwks_cache_timestamp
    async with httpx.AsyncClient() as client:
        response = await client.get(settings.supabase_jwks_url)
        response.raise_for_status()
        jwks = response.json()
        _jwks_cache.clear()
        _jwks_cache.update(jwks)
        _jwks_cache_timestamp = time.monotonic()
        return jwks


async def get_jwks(force_refresh: bool = False) -> dict[str, Any]:
    """Fetch and cache JWKS from Supabase with a 1-hour TTL."""
    cache_age = time.monotonic() - _jwks_cache_timestamp
    if _jwks_cache and not force_refresh and cache_age < _JWKS_CACHE_TTL_SECONDS:
        return _jwks_cache

    return await _fetch_jwks()


def get_signing_key(jwks: dict[str, Any], token: str) -> ECKey:
    """Get the signing key from JWKS that matches the token's kid."""
    unverified_header = jwt.get_unverified_header(token)
    kid = unverified_header.get("kid")

    for key in jwks.get("keys", []):
        if key.get("kid") == kid:
            return ECKey(key, algorithm="ES256")

    raise ValueError("Unable to find matching key in JWKS")


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User:
    """
    Validate Supabase JWT and return current user.

    Creates user record on first API call if not exists.
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    token = credentials.credentials

    try:
        jwks = await get_jwks()
        signing_key = get_signing_key(jwks, token)

        payload = jwt.decode(
            token,
            signing_key,
            algorithms=["ES256"],
            audience="authenticated",
        )
        user_id_str: str | None = payload.get("sub")
        if user_id_str is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid authentication token",
            )
        user_id = uuid_pkg.UUID(user_id_str)
    except (JWTError, ValueError) as first_error:
        # Key rotation may have occurred — force a JWKS refresh and retry once
        try:
            logger.info("JWT validation failed with cached JWKS, forcing refresh")
            jwks = await get_jwks(force_refresh=True)
            signing_key = get_signing_key(jwks, token)
            payload = jwt.decode(token, signing_key, algorithms=["ES256"], audience="authenticated")
            user_id_str = payload.get("sub")
            if user_id_str is None:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid authentication token",
                ) from None
            user_id = uuid_pkg.UUID(user_id_str)
        except (JWTError, ValueError, httpx.HTTPError):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not validate credentials",
            ) from first_error
    except httpx.HTTPError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
        ) from None

    # RLS context must be established before the user-row lookup. The lookup
    # itself queries an RLS-protected table, so without app.current_user_id
    # set, app_user_id() returns NULL and the user's own row is filtered out.
    # (Pre-trajan_app this was hidden by the postgres role's BYPASSRLS.)
    await set_rls_user_context(db, user_id)

    # Get or create user
    statement = select(User).where(User.id == user_id)
    result = await db.execute(statement)
    user = result.scalar_one_or_none()

    # Extract metadata for potential user creation
    app_metadata = payload.get("app_metadata", {})
    user_metadata = payload.get("user_metadata", {})

    if not user:
        # Create user on first API call (fallback if trigger didn't run)
        auth_provider = app_metadata.get("provider", "email")

        user = User(
            id=user_id,
            email=payload.get("email"),
            github_username=user_metadata.get("user_name"),
            avatar_url=user_metadata.get("avatar_url"),
            auth_provider=auth_provider,
        )
        db.add(user)
        await db.flush()
        await db.refresh(user)

    return user


async def get_current_user_optional(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> User | None:
    """Get current user if authenticated, None otherwise."""
    if not credentials:
        return None
    try:
        return await get_current_user(credentials, db)
    except HTTPException:
        return None


# Type aliases for cleaner dependency injection
DbSession = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_db_with_rls(
    db: DbSession,
    current_user: CurrentUser,
) -> AsyncGenerator[AsyncSession, None]:
    """
    Database session with RLS user context automatically set.

    This dependency combines get_db and get_current_user, then sets the
    PostgreSQL session variable that RLS policies use to identify the user.

    The RLS context uses SET LOCAL, which is transaction-scoped and
    automatically cleared when the transaction ends. This works correctly
    with connection poolers like PgBouncer.

    Usage:
        @router.get("/protected")
        async def endpoint(db: AsyncSession = Depends(get_db_with_rls)):
            # All queries are now filtered by RLS policies
            products = await db.execute(select(Product))

    Note: Use regular get_db for public endpoints or admin operations
    that should bypass RLS (when using service role connection).
    """
    await set_rls_user_context(db, current_user.id)
    yield db
