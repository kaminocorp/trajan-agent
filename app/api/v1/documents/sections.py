"""Document sections API endpoints.

Provides CRUD operations for document sections and subsections,
plus reordering and document movement between sections.
"""

import uuid as uuid_pkg

from fastapi import Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import (
    check_product_editor_access,
    get_current_user,
    get_db_with_rls,
    get_product_access,
)
from app.domain import document_ops, section_ops, subsection_ops
from app.models.document_section import (
    DocumentSectionCreate,
    DocumentSubsectionCreate,
)
from app.models.user import User

# =============================================================================
# Request/Response Schemas
# =============================================================================


class SubsectionResponse(BaseModel):
    """Subsection in API responses."""

    id: str
    section_id: str
    name: str
    slug: str
    position: int
    is_default: bool
    created_at: str
    updated_at: str


class SectionResponse(BaseModel):
    """Section with subsections in API responses."""

    id: str
    product_id: str
    name: str
    slug: str
    position: int
    color: str | None
    icon: str | None
    is_default: bool
    subsections: list[SubsectionResponse]
    created_at: str
    updated_at: str


class CreateSectionRequest(BaseModel):
    """Request body for creating a section."""

    name: str
    slug: str
    color: str | None = None
    icon: str | None = None


class UpdateSectionRequest(BaseModel):
    """Request body for updating a section."""

    name: str | None = None
    slug: str | None = None
    color: str | None = None
    icon: str | None = None


class CreateSubsectionRequest(BaseModel):
    """Request body for creating a subsection."""

    name: str
    slug: str


class UpdateSubsectionRequest(BaseModel):
    """Request body for updating a subsection."""

    name: str | None = None
    slug: str | None = None


class ReorderRequest(BaseModel):
    """Request body for reordering sections or subsections."""

    ids: list[str]


class MoveDocumentRequest(BaseModel):
    """Request body for moving a document to a section/subsection."""

    section_id: str | None = None
    subsection_id: str | None = None


# =============================================================================
# Serializers
# =============================================================================


def serialize_subsection(s) -> SubsectionResponse:
    """Serialize a DocumentSubsection to response format."""
    return SubsectionResponse(
        id=str(s.id),
        section_id=str(s.section_id),
        name=s.name,
        slug=s.slug,
        position=s.position,
        is_default=s.is_default,
        created_at=s.created_at.isoformat(),
        updated_at=s.updated_at.isoformat(),
    )


def serialize_section(s) -> SectionResponse:
    """Serialize a DocumentSection to response format."""
    return SectionResponse(
        id=str(s.id),
        product_id=str(s.product_id),
        name=s.name,
        slug=s.slug,
        position=s.position,
        color=s.color,
        icon=s.icon,
        is_default=s.is_default,
        subsections=[serialize_subsection(sub) for sub in (s.subsections or [])],
        created_at=s.created_at.isoformat(),
        updated_at=s.updated_at.isoformat(),
    )


# =============================================================================
# Section Endpoints
# =============================================================================


async def list_sections(
    product_id: uuid_pkg.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> list[SectionResponse]:
    """Get all sections for a product with subsections.

    Creates default sections (Technical, Conceptual) if none exist.
    """
    await get_product_access(product_id, db, current_user)

    # Ensure default sections exist
    sections = await section_ops.ensure_default_sections(db, product_id)
    await db.commit()

    return [serialize_section(s) for s in sections]


async def create_section(
    product_id: uuid_pkg.UUID,
    data: CreateSectionRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> SectionResponse:
    """Create a new section. Requires Editor access."""
    await check_product_editor_access(db, product_id, current_user.id)

    # Check for slug uniqueness
    existing = await section_ops.get_by_slug(db, product_id, data.slug)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Section with slug '{data.slug}' already exists",
        )

    # Get next position
    position = await section_ops.get_next_position(db, product_id)

    section = await section_ops.create(
        db,
        DocumentSectionCreate(
            product_id=product_id,
            name=data.name,
            slug=data.slug,
            position=position,
            color=data.color,
            icon=data.icon,
            is_default=False,
        ),
    )
    await db.commit()

    # Re-fetch to get subsections (empty for new section)
    refreshed = await section_ops.get(db, section.id)
    return serialize_section(refreshed)


async def update_section(
    section_id: uuid_pkg.UUID,
    data: UpdateSectionRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> SectionResponse:
    """Update a section. Requires Editor access."""
    section = await section_ops.get(db, section_id)
    if not section:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Section not found",
        )

    await check_product_editor_access(db, section.product_id, current_user.id)

    # Check slug uniqueness if changing
    if data.slug and data.slug != section.slug:
        existing = await section_ops.get_by_slug(db, section.product_id, data.slug)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Section with slug '{data.slug}' already exists",
            )

    updated = await section_ops.update(db, section, data.model_dump(exclude_unset=True))
    await db.commit()

    return serialize_section(updated)


async def delete_section(
    section_id: uuid_pkg.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """Delete a section. Default sections cannot be deleted. Requires Editor access."""
    section = await section_ops.get(db, section_id)
    if not section:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Section not found",
        )

    await check_product_editor_access(db, section.product_id, current_user.id)

    if section.is_default:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete default sections",
        )

    await section_ops.delete(db, section_id)
    await db.commit()


async def reorder_sections(
    product_id: uuid_pkg.UUID,
    data: ReorderRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> list[SectionResponse]:
    """Reorder sections. Requires Editor access."""
    await check_product_editor_access(db, product_id, current_user.id)

    section_ids = [uuid_pkg.UUID(id) for id in data.ids]
    sections = await section_ops.reorder(db, product_id, section_ids)
    await db.commit()

    return [serialize_section(s) for s in sections]


# =============================================================================
# Subsection Endpoints
# =============================================================================


async def create_subsection(
    section_id: uuid_pkg.UUID,
    data: CreateSubsectionRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> SubsectionResponse:
    """Create a new subsection within a section. Requires Editor access."""
    section = await section_ops.get(db, section_id)
    if not section:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Section not found",
        )

    await check_product_editor_access(db, section.product_id, current_user.id)

    # Check for slug uniqueness within section
    existing = await subsection_ops.get_by_slug(db, section_id, data.slug)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Subsection with slug '{data.slug}' already exists in this section",
        )

    # Get next position
    position = await subsection_ops.get_next_position(db, section_id)

    subsection = await subsection_ops.create(
        db,
        DocumentSubsectionCreate(
            section_id=section_id,
            name=data.name,
            slug=data.slug,
            position=position,
            is_default=False,
        ),
    )
    await db.commit()

    return serialize_subsection(subsection)


async def update_subsection(
    subsection_id: uuid_pkg.UUID,
    data: UpdateSubsectionRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> SubsectionResponse:
    """Update a subsection. Requires Editor access."""
    subsection = await subsection_ops.get(db, subsection_id)
    if not subsection:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Subsection not found",
        )

    # Get parent section for product access check
    section = await section_ops.get(db, subsection.section_id)
    if not section:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Parent section not found",
        )

    await check_product_editor_access(db, section.product_id, current_user.id)

    # Check slug uniqueness if changing
    if data.slug and data.slug != subsection.slug:
        existing = await subsection_ops.get_by_slug(db, subsection.section_id, data.slug)
        if existing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Subsection with slug '{data.slug}' already exists in this section",
            )

    updated = await subsection_ops.update(db, subsection, data.model_dump(exclude_unset=True))
    await db.commit()

    return serialize_subsection(updated)


async def delete_subsection(
    subsection_id: uuid_pkg.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """Delete a subsection. Default subsections cannot be deleted. Requires Editor access."""
    subsection = await subsection_ops.get(db, subsection_id)
    if not subsection:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Subsection not found",
        )

    # Get parent section for product access check
    section = await section_ops.get(db, subsection.section_id)
    if not section:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Parent section not found",
        )

    await check_product_editor_access(db, section.product_id, current_user.id)

    if subsection.is_default:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete default subsections",
        )

    await subsection_ops.delete(db, subsection_id)
    await db.commit()


async def reorder_subsections(
    section_id: uuid_pkg.UUID,
    data: ReorderRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> list[SubsectionResponse]:
    """Reorder subsections within a section. Requires Editor access."""
    section = await section_ops.get(db, section_id)
    if not section:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Section not found",
        )

    await check_product_editor_access(db, section.product_id, current_user.id)

    subsection_ids = [uuid_pkg.UUID(id) for id in data.ids]
    subsections = await subsection_ops.reorder(db, section_id, subsection_ids)
    await db.commit()

    return [serialize_subsection(s) for s in subsections]


# =============================================================================
# Document Movement
# =============================================================================


async def move_document_to_section(
    document_id: uuid_pkg.UUID,
    data: MoveDocumentRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> dict:
    """Move a document to a different section/subsection. Requires Editor access."""
    doc = await document_ops.get(db, document_id)
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found",
        )

    if not doc.product_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Document must belong to a product",
        )

    await check_product_editor_access(db, doc.product_id, current_user.id)

    # Validate section_id if provided
    section_id = uuid_pkg.UUID(data.section_id) if data.section_id else None
    subsection_id = uuid_pkg.UUID(data.subsection_id) if data.subsection_id else None

    if section_id:
        section = await section_ops.get(db, section_id)
        if not section:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Section not found",
            )
        if section.product_id != doc.product_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Section belongs to a different product",
            )

    if subsection_id:
        subsection = await subsection_ops.get(db, subsection_id)
        if not subsection:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Subsection not found",
            )
        # Validate subsection belongs to the section
        if section_id and subsection.section_id != section_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Subsection does not belong to the specified section",
            )
        # If no section_id provided, infer from subsection
        if not section_id:
            section_id = subsection.section_id

    # Update document
    update_data = {
        "section_id": section_id,
        "subsection_id": subsection_id,
    }
    await document_ops.update(db, doc, update_data)
    await db.commit()

    return {
        "success": True,
        "section_id": str(section_id) if section_id else None,
        "subsection_id": str(subsection_id) if subsection_id else None,
    }
