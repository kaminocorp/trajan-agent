"""API key authentication dependencies for public endpoints.

Validates API keys from ``Authorization: Bearer trj_pk_xxx`` headers
and enforces scope-based access control.

Bypass-then-scope (Phase 3.0 of cron-role plan):

``product_api_keys`` is RLS-protected with a SELECT policy that requires
``app_user_id()`` to be set. Under ``trajan_app`` at request entry there
is no user context yet — validating the key on the pooled app engine
would return zero rows and 401 every request.

We validate on ``cron_session_maker`` (BYPASSRLS, narrow lookup) and
return the ``ProductApiKey`` record. Downstream handlers open their own
``async_session_maker`` session and call ``set_rls_user_context`` with
``api_key.created_by_user_id`` (NOT NULL) before any RLS-protected read
or write.

The dependency deliberately has no ``Depends(get_db)`` so the DI graph
cannot silently pull the BYPASSRLS connection into a request handler.
``grep 'cron_session_maker('`` should only find matches in the scheduler,
webhook handlers, and these two auth dependencies.
"""

from collections.abc import Awaitable, Callable

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.database import cron_session_maker
from app.domain.product_api_key_operations import ProductApiKeyOperations, api_key_ops
from app.models.product_api_key import ProductApiKey

api_key_security = HTTPBearer(auto_error=False)


async def get_api_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(api_key_security),
) -> ProductApiKey:
    """Validate API key from Bearer token and return the key record.

    Validation runs on ``cron_session_maker`` (BYPASSRLS) — see module
    docstring. Handlers consuming this dependency must open their own
    scoped session and set RLS context to ``api_key.created_by_user_id``
    before any RLS-protected read.
    """
    if not credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key required",
        )

    async with cron_session_maker() as cron_db:
        api_key = await api_key_ops.validate_key(cron_db, credentials.credentials)

    if api_key is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked API key",
        )

    return api_key


def require_scope(scope: str) -> Callable[..., Awaitable[ProductApiKey]]:
    """Return a dependency that validates an API key has the required scope."""

    async def _check_scope(
        api_key: ProductApiKey = Depends(get_api_key),
    ) -> ProductApiKey:
        if not ProductApiKeyOperations.check_scope(api_key, scope):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"API key missing required scope: {scope}",
            )
        return api_key

    return _check_scope
