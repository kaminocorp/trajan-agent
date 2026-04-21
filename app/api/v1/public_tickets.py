"""Public ticket API endpoints.

Authenticated by API key (not JWT). Allows external services to create,
interpret, and query tickets via the public API.
"""

import logging
import uuid as uuid_pkg
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select
from sqlmodel import Field, SQLModel

from app.api.deps.api_key_auth import require_scope
from app.core.database import async_session_maker
from app.core.rate_limit import (
    PUBLIC_INTERPRET_LIMIT,
    PUBLIC_READ_LIMIT,
    PUBLIC_WRITE_LIMIT,
    rate_limiter,
)
from app.core.rls import set_rls_user_context
from app.domain.work_item_operations import work_item_ops
from app.models.product_api_key import ProductApiKey
from app.models.work_item import WorkItem
from app.services.dedup import find_duplicate_work_item

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/public/tickets", tags=["Public API"])

# ---------------------------------------------------------------------------
# Priority mapping
# ---------------------------------------------------------------------------

PRIORITY_MAP = {"low": 1, "medium": 2, "high": 3, "critical": 4}
VALID_TYPES = {"feature", "fix", "refactor", "investigation", "bug", "task", "question"}

# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class PublicTicketCreate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    title: str = Field(max_length=500)
    description: str | None = Field(default=None, max_length=50_000)
    type: str | None = None
    priority: str | None = None
    reporter_email: str | None = Field(default=None, max_length=255)
    reporter_name: str | None = Field(default=None, max_length=255)
    tags: list[str] | None = None
    extra_metadata: dict[str, Any] | None = Field(default=None, alias="metadata")


class PublicTicketInterpret(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    message: str = Field(max_length=50_000)
    title: str | None = Field(default=None, max_length=500)
    reporter_email: str | None = Field(default=None, max_length=255)
    reporter_name: str | None = Field(default=None, max_length=255)
    extra_metadata: dict[str, Any] | None = Field(default=None, alias="metadata")


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class PublicTicketResponse(SQLModel):
    """Returned on create (201)."""

    id: uuid_pkg.UUID
    title: str
    status: str
    type: str | None = None
    priority: int | None = None
    created_at: datetime


class PublicTicketInterpretResponse(PublicTicketResponse):
    """Returned on interpret create (201)."""

    confidence: float


class PublicTicketDuplicate(SQLModel):
    """Returned when duplicate detected (200)."""

    duplicate: bool = True
    existing_ticket_id: uuid_pkg.UUID
    existing_ticket_title: str
    existing_ticket_status: str | None = None


class PublicTicketDetail(SQLModel):
    """Full ticket detail (GET single)."""

    id: uuid_pkg.UUID
    title: str
    description: str | None = None
    type: str | None = None
    status: str | None = None
    priority: int | None = None
    tags: list[str] | None = None
    source: str | None = None
    reporter_email: str | None = None
    reporter_name: str | None = None
    created_at: datetime
    updated_at: datetime | None = None


class PublicTicketList(SQLModel):
    """List item (GET list) — no description."""

    id: uuid_pkg.UUID
    title: str
    type: str | None = None
    status: str | None = None
    priority: int | None = None
    source: str | None = None
    created_at: datetime
    updated_at: datetime | None = None


class PublicTicketListResponse(SQLModel):
    """Paginated list response."""

    items: list[PublicTicketList]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/",
    response_model=PublicTicketResponse | PublicTicketDuplicate,
    status_code=status.HTTP_201_CREATED,
)
async def create_ticket(
    data: PublicTicketCreate,
    response: Response,
    api_key: ProductApiKey = Depends(require_scope("tickets:write")),
) -> PublicTicketResponse | PublicTicketDuplicate:
    """Create a ticket with structured fields."""
    rate_limiter.check_rate_limit(api_key.id, "public_write", PUBLIC_WRITE_LIMIT)

    # Validate type if provided
    if data.type and data.type not in VALID_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid type '{data.type}'. Must be one of: {', '.join(sorted(VALID_TYPES))}",
        )

    # Validate priority if provided
    if data.priority and data.priority not in PRIORITY_MAP:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Invalid priority '{data.priority}'. "
                f"Must be one of: {', '.join(PRIORITY_MAP.keys())}"
            ),
        )

    async with async_session_maker() as db:
        await set_rls_user_context(db, api_key.created_by_user_id)

        # Check for duplicates — RLS-protected read, must run after set_rls_user_context
        duplicate = await find_duplicate_work_item(db, api_key.product_id, data.title)
        if duplicate:
            response.status_code = status.HTTP_200_OK
            return PublicTicketDuplicate(
                existing_ticket_id=duplicate.id,
                existing_ticket_title=duplicate.title or "",
                existing_ticket_status=duplicate.status,
            )

        work_item = await work_item_ops.create(
            db,
            obj_in={
                "product_id": api_key.product_id,
                "title": data.title,
                "description": data.description,
                "type": data.type,
                "status": "reported",
                "priority": PRIORITY_MAP.get(data.priority, 3) if data.priority else 3,
                "source": "api",
                "reporter_email": data.reporter_email,
                "reporter_name": data.reporter_name,
                "tags": data.tags,
                "ticket_metadata": data.extra_metadata,
            },
            created_by_user_id=api_key.created_by_user_id,
        )
        await db.commit()

        return PublicTicketResponse(
            id=work_item.id,
            title=work_item.title or "",
            status=work_item.status or "reported",
            type=work_item.type,
            priority=work_item.priority,
            created_at=work_item.created_at,
        )


@router.post(
    "/interpret",
    response_model=PublicTicketInterpretResponse | PublicTicketDuplicate,
    status_code=status.HTTP_201_CREATED,
)
async def interpret_ticket(
    data: PublicTicketInterpret,
    response: Response,
    api_key: ProductApiKey = Depends(require_scope("tickets:write")),
) -> PublicTicketInterpretResponse | PublicTicketDuplicate:
    """Interpret a raw message via AI and create a structured ticket."""
    rate_limiter.check_rate_limit(api_key.id, "public_interpret", PUBLIC_INTERPRET_LIMIT)

    from app.services.interpreter import MessageInput, MessageToTicketInterpreter

    # Interpret via AI (no DB work — safe to run before opening the scoped session)
    message_input = MessageInput(
        content=data.message,
        title=data.title,
        source="api_webhook",
        user_email=data.reporter_email,
        metadata=data.extra_metadata or {},
    )
    interpreter = MessageToTicketInterpreter()
    ticket = await interpreter.interpret(message_input)

    title = ticket.summary.split(". ")[0][:500]
    description_parts = [ticket.summary]
    if ticket.acceptance_criteria:
        description_parts.append("\n\nAcceptance Criteria:")
        for criterion in ticket.acceptance_criteria:
            description_parts.append(f"- {criterion}")
    description = "\n".join(description_parts)

    async with async_session_maker() as db:
        await set_rls_user_context(db, api_key.created_by_user_id)

        duplicate = await find_duplicate_work_item(db, api_key.product_id, title)
        if duplicate:
            response.status_code = status.HTTP_200_OK
            return PublicTicketDuplicate(
                existing_ticket_id=duplicate.id,
                existing_ticket_title=duplicate.title or "",
                existing_ticket_status=duplicate.status,
            )

        work_item = await work_item_ops.create(
            db,
            obj_in={
                "product_id": api_key.product_id,
                "title": title,
                "description": description,
                "type": ticket.ticket_type,
                "status": "reported",
                "priority": PRIORITY_MAP.get(ticket.priority, 3),
                "source": "api_interpreted",
                "tags": ticket.suggested_labels,
                "reporter_email": data.reporter_email,
                "reporter_name": data.reporter_name,
                "ticket_metadata": data.extra_metadata,
            },
            created_by_user_id=api_key.created_by_user_id,
        )
        await db.commit()

        return PublicTicketInterpretResponse(
            id=work_item.id,
            title=work_item.title or "",
            status=work_item.status or "reported",
            type=work_item.type,
            priority=work_item.priority,
            created_at=work_item.created_at,
            confidence=ticket.confidence,
        )


@router.get("/", response_model=PublicTicketListResponse)
async def list_tickets(
    api_key: ProductApiKey = Depends(require_scope("tickets:read")),
    status_filter: str | None = Query(None, alias="status"),
    type_filter: str | None = Query(None, alias="type"),
    source: str | None = Query(None),
    q: str | None = Query(None),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> PublicTicketListResponse:
    """Query tickets with filtering and pagination."""
    rate_limiter.check_rate_limit(api_key.id, "public_read", PUBLIC_READ_LIMIT)

    base_where = [
        WorkItem.product_id == api_key.product_id,
        WorkItem.deleted_at.is_(None),  # type: ignore[union-attr]
    ]

    if status_filter:
        base_where.append(WorkItem.status == status_filter)
    if type_filter:
        base_where.append(WorkItem.type == type_filter)
    if source:
        base_where.append(WorkItem.source == source)
    if q:
        escaped_q = q.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")
        base_where.append(
            WorkItem.title.ilike(f"%{escaped_q}%", escape="\\")  # type: ignore[union-attr]
        )

    async with async_session_maker() as db:
        await set_rls_user_context(db, api_key.created_by_user_id)

        count_stmt = select(func.count()).select_from(WorkItem).where(*base_where)
        total_result = await db.execute(count_stmt)
        total = total_result.scalar_one()

        data_stmt = (
            select(WorkItem)
            .where(*base_where)
            .order_by(WorkItem.created_at.desc())  # type: ignore[attr-defined]
            .limit(limit)
            .offset(offset)
        )
        result = await db.execute(data_stmt)
        items = result.scalars().all()

        return PublicTicketListResponse(
            items=[
                PublicTicketList(
                    id=item.id,
                    title=item.title or "",
                    type=item.type,
                    status=item.status,
                    priority=item.priority,
                    source=item.source,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                )
                for item in items
            ],
            total=total,
            limit=limit,
            offset=offset,
        )


@router.get("/{ticket_id}", response_model=PublicTicketDetail)
async def get_ticket(
    ticket_id: uuid_pkg.UUID,
    api_key: ProductApiKey = Depends(require_scope("tickets:read")),
) -> PublicTicketDetail:
    """Get a single ticket by ID."""
    rate_limiter.check_rate_limit(api_key.id, "public_read", PUBLIC_READ_LIMIT)

    async with async_session_maker() as db:
        await set_rls_user_context(db, api_key.created_by_user_id)

        statement = select(WorkItem).where(
            WorkItem.id == ticket_id,  # type: ignore[arg-type]
            WorkItem.product_id == api_key.product_id,  # type: ignore[arg-type]
            WorkItem.deleted_at.is_(None),  # type: ignore[union-attr]
        )
        result = await db.execute(statement)
        work_item = result.scalar_one_or_none()

        if not work_item:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Ticket not found",
            )

        return PublicTicketDetail(
            id=work_item.id,
            title=work_item.title or "",
            description=work_item.description,
            type=work_item.type,
            status=work_item.status,
            priority=work_item.priority,
            tags=work_item.tags,
            source=work_item.source,
            reporter_email=work_item.reporter_email,
            reporter_name=work_item.reporter_name,
            created_at=work_item.created_at,
            updated_at=work_item.updated_at,
        )
