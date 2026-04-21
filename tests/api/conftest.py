"""API test fixtures — domain entities + auth variant clients.

Builds on root conftest fixtures (db_session, test_user, test_org,
test_subscription, test_product, api_client, mock_external_services).

All fixtures create entities via domain operations within the rolled-back
transaction, so they exercise the same validation as production code.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User

# ─────────────────────────────────────────────────────────────────────────────
# Domain Entity Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
async def test_repository(db_session: AsyncSession, test_user, test_product):
    """A test repository in test_product."""
    from app.domain.repository_operations import repository_ops

    repo = await repository_ops.create(
        db_session,
        obj_in={
            "product_id": test_product.id,
            "name": f"test-repo-{uuid.uuid4().hex[:8]}",
            "full_name": f"test-org/test-repo-{uuid.uuid4().hex[:8]}",
            "description": "Test repository",
        },
        imported_by_user_id=test_user.id,
    )
    return repo


@pytest.fixture
async def test_document(db_session: AsyncSession, test_user, test_product):
    """A test document in test_product."""
    from app.domain.document_operations import document_ops

    doc = await document_ops.create(
        db_session,
        obj_in={
            "product_id": test_product.id,
            "title": f"Test Document {uuid.uuid4().hex[:8]}",
            "content": "Test content for document.",
            "type": "note",
            "is_generated": True,
        },
        created_by_user_id=test_user.id,
    )
    return doc


@pytest.fixture
async def test_work_item(db_session: AsyncSession, test_user, test_product):
    """A test work item in test_product."""
    from app.domain.work_item_operations import work_item_ops

    item = await work_item_ops.create(
        db_session,
        obj_in={
            "product_id": test_product.id,
            "title": f"Test Work Item {uuid.uuid4().hex[:8]}",
            "description": "Test work item description.",
            "type": "feature",
            "status": "reported",
        },
        created_by_user_id=test_user.id,
    )
    return item


@pytest.fixture
async def test_app_info_entry(db_session: AsyncSession, test_user, test_product):
    """A test app info entry in test_product."""
    from app.domain.app_info_operations import app_info_ops

    entry = await app_info_ops.create(
        db_session,
        obj_in={
            "product_id": test_product.id,
            "key": f"TEST_KEY_{uuid.uuid4().hex[:8]}",
            "value": "test_value",
            "category": "env_var",
            "is_secret": False,
            "tags": ["backend"],
        },
        user_id=test_user.id,
    )
    return entry


@pytest.fixture
async def test_feedback(db_session: AsyncSession, test_user):
    """A test feedback item submitted by test_user."""
    from app.domain.feedback_operations import feedback_ops
    from app.models.feedback import FeedbackCreate

    feedback = await feedback_ops.create_feedback(
        db_session,
        user_id=test_user.id,
        data=FeedbackCreate(
            type="bug",
            title=f"Test Bug {uuid.uuid4().hex[:8]}",
            description="Something is broken in tests.",
        ),
    )
    return feedback


@pytest.fixture
async def test_referral_code(db_session: AsyncSession, test_user):
    """A test referral code owned by test_user."""
    from app.domain.referral_operations import referral_ops

    code = await referral_ops.create_code(db_session, test_user.id)
    return code


# ─────────────────────────────────────────────────────────────────────────────
# Auth Variant Fixtures
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
async def admin_user(db_session: AsyncSession):
    """A system admin user."""
    user = User(
        id=uuid.uuid4(),
        email=f"__test_admin_{uuid.uuid4().hex[:8]}@example.com",
        display_name="Admin User",
        is_admin=True,
        created_at=datetime.now(UTC),
    )
    db_session.add(user)
    await db_session.flush()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def admin_client(db_session: AsyncSession, admin_user):
    """HTTP client authenticated as a system admin."""
    from app.api.deps.auth import get_current_user, get_db_with_rls
    from app.core.database import get_db
    from app.main import app

    app.dependency_overrides[get_current_user] = lambda: admin_user

    async def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db

    async def override_db_rls():
        yield db_session

    app.dependency_overrides[get_db_with_rls] = override_db_rls

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=True
    ) as client:
        yield client

    app.dependency_overrides.clear()


@pytest.fixture
async def second_user(db_session: AsyncSession):
    """A user who is NOT a member of test_org — for 403 access tests."""
    user = User(
        id=uuid.uuid4(),
        email=f"__test_second_{uuid.uuid4().hex[:8]}@example.com",
        display_name="Second User",
        created_at=datetime.now(UTC),
    )
    db_session.add(user)
    await db_session.flush()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def second_user_client(db_session: AsyncSession, second_user):
    """HTTP client authenticated as second_user (not in test_org)."""
    from app.api.deps.auth import get_current_user, get_db_with_rls
    from app.core.database import get_db
    from app.main import app

    app.dependency_overrides[get_current_user] = lambda: second_user

    async def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db

    async def override_db_rls():
        yield db_session

    app.dependency_overrides[get_db_with_rls] = override_db_rls

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=True
    ) as client:
        yield client

    app.dependency_overrides.clear()


@pytest.fixture
async def unauth_client(db_session: AsyncSession):
    """HTTP client with NO auth override — for 401 tests.

    Does NOT override get_current_user.  Without an Authorization header
    the real HTTPBearer dependency returns None, causing get_current_user
    to raise HTTPException(401).
    """
    from app.core.database import get_db
    from app.main import app

    async def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=True
    ) as client:
        yield client

    app.dependency_overrides.clear()


@pytest.fixture
async def viewer_user(db_session: AsyncSession, test_org, test_product):
    """A user with viewer role in test_org and viewer access to test_product."""
    from app.domain.org_member_operations import org_member_ops
    from app.models.product_access import ProductAccess

    user = User(
        id=uuid.uuid4(),
        email=f"__test_viewer_{uuid.uuid4().hex[:8]}@example.com",
        display_name="Viewer User",
        created_at=datetime.now(UTC),
    )
    db_session.add(user)
    await db_session.flush()
    await db_session.refresh(user)

    # Add as viewer member of test_org
    await org_member_ops.add_member(
        db_session,
        organization_id=test_org.id,
        user_id=user.id,
        role="viewer",
    )
    await db_session.flush()

    # Grant viewer-level product access
    pa = ProductAccess(
        product_id=test_product.id,
        user_id=user.id,
        access_level="viewer",
    )
    db_session.add(pa)
    await db_session.flush()

    return user


@pytest.fixture
async def viewer_client(db_session: AsyncSession, viewer_user):
    """HTTP client authenticated as viewer_user (viewer role in test_org)."""
    from app.api.deps.auth import get_current_user, get_db_with_rls
    from app.core.database import get_db
    from app.main import app

    app.dependency_overrides[get_current_user] = lambda: viewer_user

    async def override_db():
        yield db_session

    app.dependency_overrides[get_db] = override_db

    async def override_db_rls():
        yield db_session

    app.dependency_overrides[get_db_with_rls] = override_db_rls

    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=True
    ) as client:
        yield client

    app.dependency_overrides.clear()


# ─────────────────────────────────────────────────────────────────────────────
# Opt-in RLS-aware variants of the auth-variant clients (Phase B)
#
# Parallel to `rls_api_client` in the root conftest. Each wraps its bypass
# counterpart and flips the session role to `trajan_app` with
# `app.current_user_id` seeded to the correct user for that variant.
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
async def rls_admin_client(admin_client, db_session: AsyncSession, admin_user):
    """RLS-aware variant of `admin_client` — endpoint queries run under `trajan_app`."""
    from tests.conftest import _activate_trajan_app_role

    await _activate_trajan_app_role(db_session, admin_user.id)
    yield admin_client


@pytest.fixture
async def rls_second_user_client(second_user_client, db_session: AsyncSession, second_user):
    """RLS-aware variant of `second_user_client` — endpoint queries run under `trajan_app`."""
    from tests.conftest import _activate_trajan_app_role

    await _activate_trajan_app_role(db_session, second_user.id)
    yield second_user_client


@pytest.fixture
async def rls_viewer_client(viewer_client, db_session: AsyncSession, viewer_user):
    """RLS-aware variant of `viewer_client` — endpoint queries run under `trajan_app`."""
    from tests.conftest import _activate_trajan_app_role

    await _activate_trajan_app_role(db_session, viewer_user.id)
    yield viewer_client
