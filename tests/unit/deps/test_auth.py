"""Unit tests for auth dependencies — JWT validation and user auto-creation."""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.api.deps.auth import (
    get_current_user,
    get_current_user_optional,
    get_signing_key,
)
from tests.helpers.mock_factories import make_mock_user, mock_scalar_result

# ---------------------------------------------------------------------------
# get_signing_key
# ---------------------------------------------------------------------------


class TestGetSigningKey:
    """Tests for JWKS key matching by kid."""

    def test_returns_key_when_kid_matches(self):
        jwks = {
            "keys": [
                {"kid": "key-1", "kty": "EC", "crv": "P-256", "x": "a", "y": "b"},
                {"kid": "key-2", "kty": "EC", "crv": "P-256", "x": "c", "y": "d"},
            ]
        }
        token = "dummy"

        with patch("app.api.deps.auth.jwt") as mock_jwt:
            mock_jwt.get_unverified_header.return_value = {"kid": "key-2"}

            with patch("app.api.deps.auth.ECKey") as mock_eckey:
                expected_key = MagicMock()
                mock_eckey.return_value = expected_key
                key = get_signing_key(jwks, token)
                assert key == expected_key

    def test_raises_when_no_matching_kid(self):
        jwks = {"keys": [{"kid": "key-1", "kty": "EC", "crv": "P-256"}]}

        with patch("app.api.deps.auth.jwt") as mock_jwt:
            mock_jwt.get_unverified_header.return_value = {"kid": "missing-kid"}

            with pytest.raises(ValueError, match="Unable to find matching key"):
                get_signing_key(jwks, "dummy")

    def test_raises_when_no_keys_in_jwks(self):
        with patch("app.api.deps.auth.jwt") as mock_jwt:
            mock_jwt.get_unverified_header.return_value = {"kid": "any"}

            with pytest.raises(ValueError, match="Unable to find matching key"):
                get_signing_key({}, "dummy")


# ---------------------------------------------------------------------------
# get_current_user
# ---------------------------------------------------------------------------


class TestGetCurrentUser:
    """Tests for JWT-based user authentication."""

    def setup_method(self):
        self.db = AsyncMock()
        self.user_id = uuid.uuid4()
        self.valid_payload = {
            "sub": str(self.user_id),
            "email": "test@example.com",
            "app_metadata": {"provider": "email"},
            "user_metadata": {"full_name": "Test User"},
        }

    @pytest.mark.asyncio
    async def test_raises_401_when_no_credentials(self):
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(credentials=None, db=self.db)
        assert exc_info.value.status_code == 401
        assert "Not authenticated" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_raises_401_on_jwt_error(self):
        credentials = MagicMock()
        credentials.credentials = "invalid.jwt.token"

        with patch("app.api.deps.auth.get_jwks", new_callable=AsyncMock) as mock_jwks:
            mock_jwks.return_value = {"keys": []}
            with patch("app.api.deps.auth.get_signing_key") as mock_get_key:
                mock_get_key.side_effect = ValueError("no key")

                with pytest.raises(HTTPException) as exc_info:
                    await get_current_user(credentials=credentials, db=self.db)
                assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_raises_401_when_sub_missing(self):
        credentials = MagicMock()
        credentials.credentials = "valid.jwt.token"

        with (
            patch("app.api.deps.auth.get_jwks", new_callable=AsyncMock) as mock_jwks,
            patch("app.api.deps.auth.get_signing_key") as mock_get_key,
            patch("app.api.deps.auth.jwt") as mock_jwt,
        ):
            mock_jwks.return_value = {"keys": [{"kid": "k1"}]}
            mock_get_key.return_value = MagicMock()
            mock_jwt.decode.return_value = {"email": "test@example.com"}  # no 'sub'

            with pytest.raises(HTTPException) as exc_info:
                await get_current_user(credentials=credentials, db=self.db)
            assert exc_info.value.status_code == 401
            assert "Invalid authentication token" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_returns_existing_user(self):
        existing_user = make_mock_user(id=self.user_id)
        credentials = MagicMock()
        credentials.credentials = "valid.jwt.token"

        self.db.execute = AsyncMock(return_value=mock_scalar_result(existing_user))

        with (
            patch("app.api.deps.auth.get_jwks", new_callable=AsyncMock) as mock_jwks,
            patch("app.api.deps.auth.get_signing_key") as mock_get_key,
            patch("app.api.deps.auth.jwt") as mock_jwt,
        ):
            mock_jwks.return_value = {"keys": []}
            mock_get_key.return_value = MagicMock()
            mock_jwt.decode.return_value = self.valid_payload

            result = await get_current_user(credentials=credentials, db=self.db)
            assert result == existing_user

    @pytest.mark.asyncio
    async def test_sets_rls_context_before_user_lookup(self):
        """0.31.0 regression: `set_rls_user_context` must be awaited BEFORE
        the user-row SELECT. Under trajan_app the SELECT is itself RLS-
        filtered; without context `app_user_id()` returns NULL, the user's
        own row is hidden, and the flow falls into the auto-create fallback
        (which also fails under trajan_app — see pending item 2 of the
        completion doc). This ordering is the load-bearing fix in 0.31.0."""
        existing_user = make_mock_user(id=self.user_id)
        credentials = MagicMock()
        credentials.credentials = "valid.jwt.token"

        call_order: list[str] = []

        async def record_set_rls(_session, _user_id):
            call_order.append("set_rls_user_context")

        async def record_execute(*_args, **_kwargs):
            call_order.append("db.execute")
            return mock_scalar_result(existing_user)

        self.db.execute = AsyncMock(side_effect=record_execute)

        with (
            patch(
                "app.api.deps.auth.set_rls_user_context",
                side_effect=record_set_rls,
            ) as mock_rls,
            patch("app.api.deps.auth.get_jwks", new_callable=AsyncMock) as mock_jwks,
            patch("app.api.deps.auth.get_signing_key") as mock_get_key,
            patch("app.api.deps.auth.jwt") as mock_jwt,
        ):
            mock_jwks.return_value = {"keys": []}
            mock_get_key.return_value = MagicMock()
            mock_jwt.decode.return_value = self.valid_payload

            await get_current_user(credentials=credentials, db=self.db)

        assert call_order, "neither set_rls_user_context nor db.execute was called"
        assert call_order[0] == "set_rls_user_context", (
            f"set_rls_user_context must precede db.execute; got {call_order}"
        )
        assert "db.execute" in call_order
        mock_rls.assert_called_once()
        # user_id must come from the JWT `sub` claim, not any DB state.
        _, passed_user_id = mock_rls.call_args.args
        assert passed_user_id == self.user_id

    @pytest.mark.asyncio
    async def test_rls_context_not_set_when_jwt_invalid(self):
        """If JWT validation fails, `set_rls_user_context` must not run —
        we don't want to bind a session to an unverified user id. The 401
        must be raised before any DB interaction."""
        credentials = MagicMock()
        credentials.credentials = "bad.jwt.token"

        with (
            patch("app.api.deps.auth.set_rls_user_context", new_callable=AsyncMock) as mock_rls,
            patch("app.api.deps.auth.get_jwks", new_callable=AsyncMock) as mock_jwks,
            patch("app.api.deps.auth.get_signing_key") as mock_get_key,
        ):
            mock_jwks.return_value = {"keys": []}
            mock_get_key.side_effect = ValueError("no key")

            with pytest.raises(HTTPException) as exc_info:
                await get_current_user(credentials=credentials, db=self.db)
            assert exc_info.value.status_code == 401

        mock_rls.assert_not_called()

    @pytest.mark.asyncio
    async def test_creates_user_when_not_found(self):
        credentials = MagicMock()
        credentials.credentials = "valid.jwt.token"

        self.db.execute = AsyncMock(return_value=mock_scalar_result(None))
        self.db.add = MagicMock()
        self.db.flush = AsyncMock()
        self.db.refresh = AsyncMock()

        with (
            patch("app.api.deps.auth.get_jwks", new_callable=AsyncMock) as mock_jwks,
            patch("app.api.deps.auth.get_signing_key") as mock_get_key,
            patch("app.api.deps.auth.jwt") as mock_jwt,
        ):
            mock_jwks.return_value = {"keys": []}
            mock_get_key.return_value = MagicMock()
            mock_jwt.decode.return_value = self.valid_payload

            result = await get_current_user(credentials=credentials, db=self.db)
            assert result is not None
            assert result.id == self.user_id
            assert result.email == "test@example.com"


# ---------------------------------------------------------------------------
# get_current_user_optional
# ---------------------------------------------------------------------------


class TestGetCurrentUserOptional:
    """Tests for optional authentication."""

    @pytest.mark.asyncio
    async def test_returns_none_when_no_credentials(self):
        db = AsyncMock()
        result = await get_current_user_optional(credentials=None, db=db)
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_on_auth_failure(self):
        db = AsyncMock()
        credentials = MagicMock()
        credentials.credentials = "bad.jwt"

        with patch(
            "app.api.deps.auth.get_current_user",
            new_callable=AsyncMock,
            side_effect=HTTPException(status_code=401, detail="fail"),
        ):
            result = await get_current_user_optional(credentials=credentials, db=db)
            assert result is None

    @pytest.mark.asyncio
    async def test_returns_user_when_authenticated(self):
        db = AsyncMock()
        user = make_mock_user()
        credentials = MagicMock()
        credentials.credentials = "valid.jwt"

        with patch(
            "app.api.deps.auth.get_current_user",
            new_callable=AsyncMock,
            return_value=user,
        ):
            result = await get_current_user_optional(credentials=credentials, db=db)
            assert result == user
