"""Domain operations for organisation-scoped API keys."""

import hashlib
import secrets
import time
import uuid as uuid_pkg
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.base_operations import BaseOperations
from app.models.org_api_key import OrgApiKey

# In-memory cache: key_hash -> last write timestamp (monotonic seconds).
# Used to debounce last_used_at updates — only write if >60s since last update.
_last_used_write_cache: dict[str, float] = {}
_LAST_USED_DEBOUNCE_SECONDS = 60


class OrgApiKeyOperations(BaseOperations[OrgApiKey]):
    """CRUD operations for OrgApiKey model."""

    def __init__(self) -> None:
        super().__init__(OrgApiKey)

    async def create_key(
        self,
        db: AsyncSession,
        organization_id: uuid_pkg.UUID,
        name: str,
        scopes: list[str],
        created_by_user_id: uuid_pkg.UUID,
    ) -> tuple[OrgApiKey, str]:
        """Create a new organisation API key.

        Generates a random key prefixed with ``trj_org_``, stores its
        SHA-256 hash, and returns the database record alongside the raw
        key (which is never persisted).
        """
        raw_key = "trj_org_" + secrets.token_urlsafe(32)
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        key_prefix = raw_key[:16]

        db_obj = OrgApiKey(
            organization_id=organization_id,
            key_hash=key_hash,
            key_prefix=key_prefix,
            name=name,
            scopes=scopes,
            created_by_user_id=created_by_user_id,
        )
        db.add(db_obj)
        await db.flush()
        await db.refresh(db_obj)
        return db_obj, raw_key

    async def list_by_org(
        self,
        db: AsyncSession,
        organization_id: uuid_pkg.UUID,
    ) -> list[OrgApiKey]:
        """List active (non-revoked) API keys for an organisation."""
        statement = (
            select(OrgApiKey)
            .where(
                OrgApiKey.organization_id == organization_id,  # type: ignore[arg-type]
                OrgApiKey.revoked_at.is_(None),  # type: ignore[union-attr]
            )
            .order_by(OrgApiKey.created_at.desc())  # type: ignore[attr-defined]
        )
        result = await db.execute(statement)
        return list(result.scalars().all())

    async def revoke(
        self,
        db: AsyncSession,
        db_obj: OrgApiKey,
    ) -> OrgApiKey:
        """Soft-delete an API key by setting revoked_at."""
        db_obj.revoked_at = datetime.now(UTC)
        db.add(db_obj)
        await db.flush()
        await db.refresh(db_obj)
        return db_obj

    async def validate_key(
        self,
        db: AsyncSession,
        raw_key: str,
    ) -> OrgApiKey | None:
        """Validate a raw API key and update last_used_at.

        Returns the OrgApiKey if valid and not revoked, otherwise None.
        """
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        statement = select(OrgApiKey).where(
            OrgApiKey.key_hash == key_hash,  # type: ignore[arg-type]
            OrgApiKey.revoked_at.is_(None),  # type: ignore[union-attr]
        )
        result = await db.execute(statement)
        api_key = result.scalar_one_or_none()
        if api_key is None:
            return None

        # Debounce last_used_at writes — only flush to DB if >60s since last update.
        now_mono = time.monotonic()
        last_write = _last_used_write_cache.get(key_hash, 0.0)
        if now_mono - last_write > _LAST_USED_DEBOUNCE_SECONDS:
            api_key.last_used_at = datetime.now(UTC)
            db.add(api_key)
            await db.flush()
            _last_used_write_cache[key_hash] = now_mono

        return api_key

    @staticmethod
    def check_scope(api_key: OrgApiKey, required_scope: str) -> bool:
        """Check whether an API key has the required scope."""
        return required_scope in api_key.scopes


# Singleton instance
org_api_key_ops = OrgApiKeyOperations()
