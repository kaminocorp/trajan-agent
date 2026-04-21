"""
ChangelogAgent - Responsible for changelog.md management.

Tier 1 (Scan & Structure):
- Imports existing changelog if found in repo
- Creates new changelog from template if none exists

Tier 2 (Maintenance):
- Adds entries when commits/releases happen
- Responds to manual "add entry" requests
"""

import logging
import uuid as uuid_pkg
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rls import set_rls_user_context
from app.domain.document_operations import document_ops
from app.models.document import Document
from app.models.product import Product
from app.services.docs.types import ChangeEntry, ChangelogResult
from app.services.github import GitHubService

logger = logging.getLogger(__name__)


class ChangelogAgent:
    """
    Agent responsible for changelog.md.

    Manages the creation, import, and maintenance of changelog documentation.
    Follows Keep a Changelog format (https://keepachangelog.com).
    """

    def __init__(
        self,
        db: AsyncSession,
        product: Product,
        created_by_user_id: uuid_pkg.UUID | None = None,
        github_service: GitHubService | None = None,
        user_id: uuid_pkg.UUID | None = None,
    ) -> None:
        self.db = db
        self.product = product
        # ``created_by_user_id`` is the *audit* user (who authored the
        # changelog row). ``user_id`` is the *acting* user whose identity
        # drives RLS context — they may differ (e.g. a teammate-editor
        # maintaining docs owned by the org founder). If ``user_id`` is
        # omitted we fall back to ``created_by_user_id`` or the product
        # owner so non-RLS callers keep working.
        self.created_by_user_id = created_by_user_id or product.user_id
        self.user_id = user_id or self.created_by_user_id
        self.github_service = github_service

    async def run(self) -> ChangelogResult:
        """
        Check for existing changelog, create if missing.

        Returns:
            ChangelogResult with action taken and the document
        """
        existing = await self._find_existing_changelog()

        if existing:
            logger.info(f"Found existing changelog for product {self.product.id}")
            return ChangelogResult(
                action="found_existing",
                document=existing,
            )

        # Generate new changelog
        logger.info(f"Creating new changelog for product {self.product.id}")
        changelog = await self._generate_changelog()
        return ChangelogResult(
            action="created",
            document=changelog,
        )

    async def _find_existing_changelog(self) -> Document | None:
        """Find existing changelog document for this product."""
        if self.product.id is None:
            return None
        return await document_ops.get_changelog(self.db, self.product.id)

    async def _generate_changelog(self) -> Document:
        """Generate a new changelog document from template."""
        template = self._get_changelog_template()

        doc = Document(
            product_id=self.product.id,
            created_by_user_id=self.created_by_user_id,
            title="Changelog",
            content=template,
            type="changelog",
            folder=None,  # Root level
        )
        self.db.add(doc)
        await self.db.commit()
        # Commit dropped SET LOCAL; re-arm before the refresh SELECT.
        await set_rls_user_context(self.db, self.user_id)
        await self.db.refresh(doc)
        return doc

    async def add_entry(
        self,
        version: str | None,
        changes: list[ChangeEntry],
    ) -> Document:
        """
        Tier 2: Add a new entry to the changelog.

        Called by maintenance triggers (commits, releases, manual).

        Args:
            version: Version string (e.g., "1.0.0") or None for [Unreleased]
            changes: List of ChangeEntry objects to add

        Returns:
            Updated changelog document
        """
        changelog = await self._find_existing_changelog()
        if not changelog:
            changelog = await self._generate_changelog()

        updated_content = self._insert_entry(
            changelog.content or "",
            version,
            changes,
        )

        changelog.content = updated_content
        changelog.updated_at = datetime.now(UTC)
        await self.db.commit()
        # Commit dropped SET LOCAL; re-arm before the refresh SELECT.
        await set_rls_user_context(self.db, self.user_id)
        await self.db.refresh(changelog)

        return changelog

    def _insert_entry(
        self,
        content: str,
        version: str | None,
        changes: list[ChangeEntry],
    ) -> str:
        """
        Insert new entries into the changelog content.

        If version is provided, creates a new version section.
        If version is None, adds to [Unreleased] section.
        """
        if not changes:
            return content

        # Group changes by category
        grouped: dict[str, list[str]] = {}
        for change in changes:
            if change.category not in grouped:
                grouped[change.category] = []
            grouped[change.category].append(change.description)

        # Build the entry content
        entry_lines: list[str] = []

        if version:
            # Create a new version section
            today = datetime.now(UTC).strftime("%Y-%m-%d")
            entry_lines.append(f"\n## [{version}] - {today}\n")
        else:
            # We'll add to [Unreleased] section
            pass

        for category, descriptions in grouped.items():
            entry_lines.append(f"\n### {category}\n")
            for desc in descriptions:
                entry_lines.append(f"\n- {desc}")
            entry_lines.append("")

        entry_text = "\n".join(entry_lines)

        if version:
            # Insert after the "---" that follows [Unreleased]
            # or at the end if no [Unreleased] section
            unreleased_end = content.find("\n---\n")
            if unreleased_end != -1:
                insert_pos = unreleased_end + 5  # After the "---\n"
                return content[:insert_pos] + entry_text + "\n" + content[insert_pos:]
            else:
                # Just append
                return content + entry_text
        else:
            # Add to [Unreleased] section
            # Find the appropriate category section under [Unreleased]
            # For simplicity, we'll just append after [Unreleased]
            unreleased_pos = content.find("## [Unreleased]")
            if unreleased_pos != -1:
                # Find the next section or end of [Unreleased]
                next_section = content.find("\n## [", unreleased_pos + 15)
                if next_section == -1:
                    next_section = content.find("\n---\n", unreleased_pos)
                if next_section == -1:
                    next_section = len(content)

                # Insert before the next section
                return content[:next_section] + "\n" + entry_text + content[next_section:]
            else:
                # No [Unreleased] section, just append
                return content + "\n## [Unreleased]\n" + entry_text

    def _get_changelog_template(self) -> str:
        """Return starter template for changelog."""
        product_name = self.product.name or "Project"
        return f"""# Changelog

All notable changes to {product_name} will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

### Added

### Changed

### Fixed

---
"""
