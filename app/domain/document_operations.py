"""Document operations with product-scoped visibility.

Documents are visible to all users with Product access (via RLS).
The `created_by_user_id` field tracks who created the document (for audit)
but does NOT control visibility.
"""

import uuid as uuid_pkg
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.document import Document


class DocumentOperations:
    """CRUD operations for Document model.

    Unlike user-owned resources, documents use Product-based access control.
    RLS policies enforce visibility based on Product access level.
    """

    def __init__(self):
        self.model = Document

    async def get(self, db: AsyncSession, id: uuid_pkg.UUID) -> Document | None:
        """Get a document by ID. RLS enforces product access."""
        statement = select(Document).where(Document.id == id)
        result = await db.execute(statement)
        return result.scalar_one_or_none()

    async def get_by_product(
        self,
        db: AsyncSession,
        product_id: uuid_pkg.UUID,
        doc_type: str | None = None,
        is_generated: bool | None = None,
        skip: int = 0,
        limit: int = 100,
    ) -> list[Document]:
        """Get documents for a product. RLS enforces access control.

        Args:
            db: Database session
            product_id: Product to get documents for
            doc_type: Optional filter by document type
            is_generated: Optional filter by origin (True=AI-generated, False=imported)
            skip: Pagination offset
            limit: Pagination limit
        """
        statement = select(Document).where(Document.product_id == product_id)

        if doc_type:
            statement = statement.where(Document.type == doc_type)

        if is_generated is not None:
            statement = statement.where(Document.is_generated == is_generated)

        statement = statement.order_by(Document.created_at.desc()).offset(skip).limit(limit)

        result = await db.execute(statement)
        return list(result.scalars().all())

    async def get_generated_by_product(
        self,
        db: AsyncSession,
        product_id: uuid_pkg.UUID,
        skip: int = 0,
        limit: int = 100,
    ) -> list[Document]:
        """Get only AI-generated documents for a product.

        This is a convenience method for filtering to is_generated=True,
        used by the orchestrator to determine what docs already exist
        when planning new documentation.
        """
        return await self.get_by_product(db, product_id, is_generated=True, skip=skip, limit=limit)

    async def get_by_product_grouped(
        self,
        db: AsyncSession,
        product_id: uuid_pkg.UUID,
    ) -> dict[str, list[Document]]:
        """Get documents grouped by folder path."""
        docs = await self.get_by_product(db, product_id)

        grouped: dict[str, list[Document]] = {
            "changelog": [],
            "blueprints": [],
            "plans": [],
            "executing": [],
            "completions": [],
            "archive": [],
        }

        for doc in docs:
            folder_path = doc.folder.get("path") if doc.folder else None

            if doc.type == "changelog":
                grouped["changelog"].append(doc)
            elif folder_path:
                # Handle nested paths (e.g., "blueprints/backend" -> "blueprints")
                root_folder = folder_path.split("/")[0]
                if root_folder in grouped:
                    grouped[root_folder].append(doc)
            else:
                # Default to blueprints for docs without folder
                grouped["blueprints"].append(doc)

        return grouped

    async def get_by_folder(
        self,
        db: AsyncSession,
        product_id: uuid_pkg.UUID,
        folder_path: str,
    ) -> list[Document]:
        """Get documents in a specific folder."""
        result = await db.execute(
            select(Document)
            .where(Document.product_id == product_id)
            .where(Document.folder["path"].astext == folder_path)  # type: ignore[index]
            .order_by(Document.updated_at.desc())
        )
        return list(result.scalars().all())

    async def get_changelog(
        self,
        db: AsyncSession,
        product_id: uuid_pkg.UUID,
    ) -> Document | None:
        """Get the changelog document for a product."""
        result = await db.execute(
            select(Document)
            .where(Document.product_id == product_id)
            .where(Document.type == "changelog")
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_synced_documents(
        self,
        db: AsyncSession,
        product_id: uuid_pkg.UUID,
    ) -> list[Document]:
        """Get documents that have been synced with GitHub."""
        result = await db.execute(
            select(Document)
            .where(Document.product_id == product_id)
            .where(Document.github_path.isnot(None))  # type: ignore[union-attr]
            .order_by(Document.updated_at.desc())
        )
        return list(result.scalars().all())

    async def get_with_local_changes(
        self,
        db: AsyncSession,
        product_id: uuid_pkg.UUID,
    ) -> list[Document]:
        """Get documents with unsynchronized local changes."""
        result = await db.execute(
            select(Document)
            .where(Document.product_id == product_id)
            .where(Document.sync_status == "local_changes")
            .order_by(Document.updated_at.desc())
        )
        return list(result.scalars().all())

    async def create(
        self,
        db: AsyncSession,
        obj_in: dict,
        created_by_user_id: uuid_pkg.UUID,
    ) -> Document:
        """Create a new document.

        Args:
            db: Database session
            obj_in: Document data (title, content, product_id, etc.)
            created_by_user_id: User who is creating this document
        """
        db_obj = Document(**obj_in, created_by_user_id=created_by_user_id)
        db.add(db_obj)
        await db.flush()
        await db.refresh(db_obj)
        return db_obj

    async def update(
        self,
        db: AsyncSession,
        db_obj: Document,
        obj_in: dict,
    ) -> Document:
        """Update an existing document."""
        for field, value in obj_in.items():
            setattr(db_obj, field, value)
        db.add(db_obj)
        await db.flush()
        await db.refresh(db_obj)
        return db_obj

    async def delete(self, db: AsyncSession, id: uuid_pkg.UUID) -> bool:
        """Delete a document by ID. Caller must verify product access."""
        db_obj = await self.get(db, id)
        if db_obj:
            await db.delete(db_obj)
            await db.flush()
            return True
        return False

    async def delete_by_product_generated(
        self,
        db: AsyncSession,
        product_id: uuid_pkg.UUID,
    ) -> int:
        """Delete all AI-generated documents for a product.

        This is a bulk delete operation for clearing all generated docs.
        Does not affect imported/repository docs (is_generated=False).

        Returns:
            Number of documents deleted.
        """
        stmt = delete(Document).where(
            Document.product_id == product_id,
            Document.is_generated == True,  # noqa: E712
        )
        result = await db.execute(stmt)
        await db.flush()
        return result.rowcount

    async def move_to_folder(
        self,
        db: AsyncSession,
        document_id: uuid_pkg.UUID,
        new_folder: str,
    ) -> Document | None:
        """Move document to a new folder."""
        doc = await self.get(db, document_id)
        if not doc:
            return None

        doc.folder = {"path": new_folder}
        doc.updated_at = datetime.now(UTC)
        # Mark as having local changes if it was synced
        if doc.sync_status == "synced":
            doc.sync_status = "local_changes"
        await db.commit()
        await db.refresh(doc)
        return doc

    async def mark_local_changes(
        self,
        db: AsyncSession,
        document_id: uuid_pkg.UUID,
    ) -> Document | None:
        """Mark a document as having local changes (for sync tracking)."""
        doc = await self.get(db, document_id)
        if not doc:
            return None

        if doc.github_path:  # Only track if it's a synced document
            doc.sync_status = "local_changes"
            await db.commit()
            await db.refresh(doc)
        return doc

    async def move_to_executing(
        self,
        db: AsyncSession,
        document_id: uuid_pkg.UUID,
    ) -> Document | None:
        """Move a plan document to the executing/ folder."""
        return await self.move_to_folder(db, document_id, "executing")

    async def move_to_completed(
        self,
        db: AsyncSession,
        document_id: uuid_pkg.UUID,
    ) -> Document | None:
        """Move a plan document to completions/ folder with date prefix."""
        date_prefix = datetime.now(UTC).strftime("%Y-%m-%d")
        return await self.move_to_folder(db, document_id, f"completions/{date_prefix}")

    async def archive(
        self,
        db: AsyncSession,
        document_id: uuid_pkg.UUID,
    ) -> Document | None:
        """Move a document to the archive/ folder."""
        return await self.move_to_folder(db, document_id, "archive")


document_ops = DocumentOperations()
