"""Section operations for document organization.

Provides CRUD operations for DocumentSection and DocumentSubsection,
plus helper methods for seeding default sections and reordering.
"""

import uuid as uuid_pkg
from datetime import UTC, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.document_section import (
    DocumentSection,
    DocumentSectionCreate,
    DocumentSubsection,
    DocumentSubsectionCreate,
)

# Default section configurations (matches frontend taxonomy)
DEFAULT_SECTIONS = [
    {
        "name": "Technical Documentation",
        "slug": "technical",
        "position": 0,
        "icon": "Code",
        "is_default": True,
        "subsections": [
            {"name": "Infrastructure/DevOps", "slug": "infrastructure", "position": 0},
            {"name": "Frontend", "slug": "frontend", "position": 1},
            {"name": "Backend", "slug": "backend", "position": 2},
            {"name": "Database", "slug": "database", "position": 3},
            {"name": "Integrations/APIs", "slug": "integrations", "position": 4},
            {"name": "Code Quality", "slug": "code-quality", "position": 5},
            {"name": "Security", "slug": "security", "position": 6},
            {"name": "Performance", "slug": "performance", "position": 7},
        ],
    },
    {
        "name": "Conceptual Documentation",
        "slug": "conceptual",
        "position": 1,
        "icon": "BookOpen",
        "is_default": True,
        "subsections": [
            {"name": "Product Overview", "slug": "overview", "position": 0},
            {"name": "Core Concepts", "slug": "concepts", "position": 1},
            {"name": "Workflows", "slug": "workflows", "position": 2},
            {"name": "Glossary", "slug": "glossary", "position": 3},
        ],
    },
]


class SectionOperations:
    """CRUD operations for DocumentSection model."""

    def __init__(self):
        self.model = DocumentSection

    async def get(self, db: AsyncSession, id: uuid_pkg.UUID) -> DocumentSection | None:
        """Get a section by ID with its subsections loaded."""
        result = await db.execute(
            select(DocumentSection)
            .options(selectinload(DocumentSection.subsections))
            .where(DocumentSection.id == id)
        )
        return result.scalar_one_or_none()

    async def get_by_product(
        self,
        db: AsyncSession,
        product_id: uuid_pkg.UUID,
    ) -> list[DocumentSection]:
        """Get all sections for a product, ordered by position."""
        result = await db.execute(
            select(DocumentSection)
            .options(selectinload(DocumentSection.subsections))
            .where(DocumentSection.product_id == product_id)
            .order_by(DocumentSection.position)
        )
        return list(result.scalars().all())

    async def get_by_slug(
        self,
        db: AsyncSession,
        product_id: uuid_pkg.UUID,
        slug: str,
    ) -> DocumentSection | None:
        """Get a section by slug within a product."""
        result = await db.execute(
            select(DocumentSection)
            .options(selectinload(DocumentSection.subsections))
            .where(DocumentSection.product_id == product_id)
            .where(DocumentSection.slug == slug)
        )
        return result.scalar_one_or_none()

    async def create(
        self,
        db: AsyncSession,
        obj_in: DocumentSectionCreate,
    ) -> DocumentSection:
        """Create a new section."""
        db_obj = DocumentSection(**obj_in.model_dump())
        db.add(db_obj)
        await db.flush()
        await db.refresh(db_obj)
        return db_obj

    async def update(
        self,
        db: AsyncSession,
        db_obj: DocumentSection,
        obj_in: dict,
    ) -> DocumentSection:
        """Update an existing section."""
        for field, value in obj_in.items():
            setattr(db_obj, field, value)
        db_obj.updated_at = datetime.now(UTC)
        db.add(db_obj)
        await db.flush()
        await db.refresh(db_obj)
        return db_obj

    async def delete(self, db: AsyncSession, id: uuid_pkg.UUID) -> bool:
        """Delete a section by ID. Cascades to subsections."""
        db_obj = await self.get(db, id)
        if db_obj:
            # Don't allow deleting default sections
            if db_obj.is_default:
                return False
            await db.delete(db_obj)
            await db.flush()
            return True
        return False

    async def reorder(
        self,
        db: AsyncSession,
        product_id: uuid_pkg.UUID,
        section_ids: list[uuid_pkg.UUID],
    ) -> list[DocumentSection]:
        """Reorder sections by setting their positions.

        Args:
            db: Database session
            product_id: Product to reorder sections for
            section_ids: List of section IDs in desired order
        """
        for position, section_id in enumerate(section_ids):
            await db.execute(
                update(DocumentSection)
                .where(DocumentSection.id == section_id)
                .where(DocumentSection.product_id == product_id)
                .values(position=position, updated_at=datetime.now(UTC))
            )
        await db.flush()
        return await self.get_by_product(db, product_id)

    async def get_next_position(
        self,
        db: AsyncSession,
        product_id: uuid_pkg.UUID,
    ) -> int:
        """Get the next available position for a new section."""
        result = await db.execute(
            select(func.max(DocumentSection.position)).where(
                DocumentSection.product_id == product_id
            )
        )
        max_pos = result.scalar_one_or_none()
        return (max_pos or 0) + 1

    async def ensure_default_sections(
        self,
        db: AsyncSession,
        product_id: uuid_pkg.UUID,
    ) -> list[DocumentSection]:
        """Create default sections for a product if none exist.

        Returns existing sections if any are present, otherwise creates
        the default Technical and Conceptual sections with subsections.
        """
        existing = await self.get_by_product(db, product_id)
        if existing:
            return existing

        created_sections = []
        for section_config in DEFAULT_SECTIONS:
            subsections = section_config.pop("subsections", [])

            section = await self.create(
                db,
                DocumentSectionCreate(
                    product_id=product_id,
                    **section_config,
                ),
            )

            # Create subsections
            for subsection_config in subsections:
                await subsection_ops.create(
                    db,
                    DocumentSubsectionCreate(
                        section_id=section.id,
                        is_default=True,
                        **subsection_config,
                    ),
                )

            # Re-fetch to get subsections
            refreshed = await self.get(db, section.id)
            if refreshed:
                created_sections.append(refreshed)

        return created_sections


class SubsectionOperations:
    """CRUD operations for DocumentSubsection model."""

    def __init__(self):
        self.model = DocumentSubsection

    async def get(self, db: AsyncSession, id: uuid_pkg.UUID) -> DocumentSubsection | None:
        """Get a subsection by ID."""
        result = await db.execute(select(DocumentSubsection).where(DocumentSubsection.id == id))
        return result.scalar_one_or_none()

    async def get_by_section(
        self,
        db: AsyncSession,
        section_id: uuid_pkg.UUID,
    ) -> list[DocumentSubsection]:
        """Get all subsections for a section, ordered by position."""
        result = await db.execute(
            select(DocumentSubsection)
            .where(DocumentSubsection.section_id == section_id)
            .order_by(DocumentSubsection.position)
        )
        return list(result.scalars().all())

    async def get_by_slug(
        self,
        db: AsyncSession,
        section_id: uuid_pkg.UUID,
        slug: str,
    ) -> DocumentSubsection | None:
        """Get a subsection by slug within a section."""
        result = await db.execute(
            select(DocumentSubsection)
            .where(DocumentSubsection.section_id == section_id)
            .where(DocumentSubsection.slug == slug)
        )
        return result.scalar_one_or_none()

    async def create(
        self,
        db: AsyncSession,
        obj_in: DocumentSubsectionCreate,
    ) -> DocumentSubsection:
        """Create a new subsection."""
        db_obj = DocumentSubsection(**obj_in.model_dump())
        db.add(db_obj)
        await db.flush()
        await db.refresh(db_obj)
        return db_obj

    async def update(
        self,
        db: AsyncSession,
        db_obj: DocumentSubsection,
        obj_in: dict,
    ) -> DocumentSubsection:
        """Update an existing subsection."""
        for field, value in obj_in.items():
            setattr(db_obj, field, value)
        db_obj.updated_at = datetime.now(UTC)
        db.add(db_obj)
        await db.flush()
        await db.refresh(db_obj)
        return db_obj

    async def delete(self, db: AsyncSession, id: uuid_pkg.UUID) -> bool:
        """Delete a subsection by ID."""
        db_obj = await self.get(db, id)
        if db_obj:
            # Don't allow deleting default subsections
            if db_obj.is_default:
                return False
            await db.delete(db_obj)
            await db.flush()
            return True
        return False

    async def reorder(
        self,
        db: AsyncSession,
        section_id: uuid_pkg.UUID,
        subsection_ids: list[uuid_pkg.UUID],
    ) -> list[DocumentSubsection]:
        """Reorder subsections within a section."""
        for position, subsection_id in enumerate(subsection_ids):
            await db.execute(
                update(DocumentSubsection)
                .where(DocumentSubsection.id == subsection_id)
                .where(DocumentSubsection.section_id == section_id)
                .values(position=position, updated_at=datetime.now(UTC))
            )
        await db.flush()
        return await self.get_by_section(db, section_id)

    async def get_next_position(
        self,
        db: AsyncSession,
        section_id: uuid_pkg.UUID,
    ) -> int:
        """Get the next available position for a new subsection."""
        result = await db.execute(
            select(func.max(DocumentSubsection.position)).where(
                DocumentSubsection.section_id == section_id
            )
        )
        max_pos = result.scalar_one_or_none()
        return (max_pos or 0) + 1


# Singleton instances
section_ops = SectionOperations()
subsection_ops = SubsectionOperations()
