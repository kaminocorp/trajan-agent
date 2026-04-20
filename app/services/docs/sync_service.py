"""
DocsSyncService - Two-way synchronization between Trajan and GitHub.

Handles:
- Importing documentation from repository docs/ folder
- Tracking sync state (SHA, timestamps)
- Detecting local vs remote changes
- Pushing documentation back to GitHub (Phase 2B)
"""

import logging
import uuid as uuid_pkg
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rls import set_rls_user_context
from app.models.document import Document
from app.models.repository import Repository
from app.services.docs.types import DocumentSyncStatus, ImportResult, SyncResult
from app.services.docs.utils import (
    extract_title,
    generate_github_path,
    infer_doc_type,
    map_path_to_folder,
)
from app.services.github import GitHubService
from app.services.github.exceptions import GitHubAPIError
from app.services.github.types import RepoTreeItem

logger = logging.getLogger(__name__)


def _ensure_aware(dt: datetime | None) -> datetime | None:
    """Ensure datetime is timezone-aware (assume UTC for naive datetimes)."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


class DocsSyncService:
    """
    Synchronize documentation between Trajan and GitHub repositories.

    This service manages:
    1. Import: Pull existing docs from repo's docs/ folder
    2. Track: Maintain SHA hashes for change detection
    3. Check: Detect when local or remote has changed
    4. Sync: Push local changes back to GitHub (Phase 2B)
    """

    def __init__(
        self,
        db: AsyncSession,
        github_service: GitHubService,
        user_id: uuid_pkg.UUID,
    ) -> None:
        self.db = db
        self.github_service = github_service
        # Acting user for RLS context re-arm after mid-flight commits.
        # See ``DocumentRefresher`` for the full rationale.
        self.user_id = user_id

    async def import_from_repo(
        self,
        repository: Repository,
    ) -> ImportResult:
        """
        Import documentation from repository's docs/ folder.

        Creates or updates Document records in database with sync tracking:
        - github_sha: Content hash for change detection
        - github_path: File path in repo
        - last_synced_at: Timestamp
        - sync_status: "synced"

        Args:
            repository: Repository to import from

        Returns:
            ImportResult with counts of imported/updated/skipped docs
        """
        if not repository.full_name:
            return ImportResult(imported=0, updated=0, skipped=0)

        owner, repo_name = repository.full_name.split("/", 1)
        branch = repository.default_branch or "main"

        # Get repo tree to find docs
        try:
            tree = await self.github_service.get_repo_tree(owner, repo_name, branch)
        except GitHubAPIError as e:
            logger.error(f"Failed to get repo tree for {repository.full_name}: {e}")
            return ImportResult(imported=0, updated=0, skipped=0)

        # Find markdown files in docs/ folder or root changelog
        docs_items = [item for item in tree.all_items if self._is_doc_file(item)]

        if not docs_items:
            logger.info(f"No documentation files found in {repository.full_name}")
            return ImportResult(imported=0, updated=0, skipped=0)

        result = ImportResult()

        for item in docs_items:
            try:
                # Check if already imported
                existing = await self._find_by_github_path(
                    repository.product_id,
                    item.path,
                )

                if existing:
                    if existing.github_sha == item.sha:
                        # No changes - skip
                        result.skipped += 1
                        continue
                    else:
                        # Content changed - update
                        await self._update_from_github(existing, repository, item, branch)
                        result.updated += 1
                        logger.info(f"Updated doc: {item.path}")
                else:
                    # New file - import
                    await self._create_from_github(repository, item, branch)
                    result.imported += 1
                    logger.info(f"Imported doc: {item.path}")

            except Exception as e:
                logger.error(f"Failed to process {item.path}: {e}")
                continue

        await self.db.commit()
        return result

    async def sync_to_repo(
        self,
        documents: list[Document],
        repository: Repository,
        commit_message: str,
    ) -> SyncResult:
        """
        Push documents to a GitHub repository, respecting sync configuration.

        Uses the repository's sync config fields to determine:
        - Which branch to commit to (sync_branch or default branch)
        - What path prefix to use (sync_path_prefix, defaults to "docs/")
        - Whether to create a branch if it doesn't exist
        - Whether to open a PR after committing

        Args:
            documents: Documents to sync
            repository: Target repository (with sync config)
            commit_message: Commit message

        Returns:
            SyncResult with success status, commit SHA, branch, and optional PR info
        """
        if not documents:
            return SyncResult(success=True, files_synced=0)

        if not repository or not repository.full_name:
            return SyncResult(
                success=False,
                files_synced=0,
                errors=["No GitHub repository linked"],
            )

        owner, repo_name = repository.full_name.split("/", 1)
        default_branch = repository.default_branch or "main"
        target_branch = repository.sync_branch or default_branch
        path_prefix = repository.sync_path_prefix or "docs/"

        try:
            # If syncing to a non-default branch, ensure it exists
            if target_branch != default_branch:
                await self.github_service.create_branch(
                    owner, repo_name, target_branch, from_branch=default_branch
                )

            # Build file list with sync path prefix
            files_to_commit = []
            for doc in documents:
                if not doc.content:
                    continue

                path = self._resolve_doc_path(doc, path_prefix)
                files_to_commit.append({"path": path, "content": doc.content})

            if not files_to_commit:
                return SyncResult(success=True, files_synced=0, branch=target_branch)

            # Create commit via GitHub API
            commit_sha = await self.github_service.create_commit(
                owner, repo_name, files_to_commit, commit_message, target_branch
            )

            # Optionally open a PR (only when syncing to a non-default branch)
            pr_url: str | None = None
            pr_number: int | None = None
            if repository.sync_create_pr and target_branch != default_branch:
                pr_info = await self.github_service.create_pull_request(
                    owner,
                    repo_name,
                    title=f"docs: sync {len(files_to_commit)} documents from Trajan",
                    body=self._build_pr_body(files_to_commit),
                    head=target_branch,
                    base=default_branch,
                )
                pr_url = pr_info.html_url
                pr_number = pr_info.number
                repository.last_sync_pr_url = pr_url

            # Update sync tracking on repository
            repository.last_sync_commit_sha = commit_sha

            # Update sync tracking on each document.
            # If per-file SHA fetch fails, mark the doc as needing re-sync
            # rather than crashing — the commit already landed on GitHub.
            for doc in documents:
                if not doc.content:
                    continue
                path = self._resolve_doc_path(doc, path_prefix)
                try:
                    new_sha = await self.github_service.get_file_sha(
                        owner, repo_name, path, target_branch
                    )
                    doc.github_sha = new_sha
                    doc.github_path = path
                    doc.last_synced_at = datetime.now(UTC)
                    doc.sync_status = "synced"
                except Exception as e:
                    logger.warning(
                        f"Failed to fetch SHA for {path} after commit — will re-sync next time: {e}"
                    )
                    doc.github_path = path
                    doc.sync_status = "local_changes"

            await self.db.commit()

            logger.info(
                f"Synced {len(files_to_commit)} files to {repository.full_name}:{target_branch}"
            )

            return SyncResult(
                success=True,
                files_synced=len(files_to_commit),
                commit_sha=commit_sha,
                branch=target_branch,
                pr_url=pr_url,
                pr_number=pr_number,
            )

        except GitHubAPIError as e:
            logger.error(f"Failed to sync to repo: {e}")
            return SyncResult(
                success=False,
                files_synced=0,
                errors=[f"Failed to sync: {e.message}"],
            )

    async def check_for_updates(
        self,
        product_id: str,
    ) -> list[DocumentSyncStatus]:
        """
        Check which documents have remote changes.

        Compares local github_sha with current SHA in repository.
        RLS enforces product access.

        Args:
            product_id: Product to check documents for

        Returns:
            List of DocumentSyncStatus for each synced document
        """
        # Get all documents with github_path (synced docs)
        result = await self.db.execute(
            select(Document)
            .where(Document.product_id == uuid_pkg.UUID(product_id))
            .where(Document.github_path != None)  # noqa: E711
        )
        docs = list(result.scalars().all())

        if not docs:
            return []

        statuses: list[DocumentSyncStatus] = []

        # Group by repository to minimize API calls
        docs_by_repo: dict[str, list[Document]] = {}
        for doc in docs:
            if doc.repository_id:
                repo_id = str(doc.repository_id)
                if repo_id not in docs_by_repo:
                    docs_by_repo[repo_id] = []
                docs_by_repo[repo_id].append(doc)

        for repo_id, repo_docs in docs_by_repo.items():
            # Get repository
            repo_result = await self.db.execute(select(Repository).where(Repository.id == repo_id))
            repo = repo_result.scalar_one_or_none()

            if not repo or not repo.full_name:
                for doc in repo_docs:
                    statuses.append(
                        DocumentSyncStatus(
                            document_id=str(doc.id),
                            status="error",
                            error="Repository not found",
                        )
                    )
                continue

            owner, repo_name = repo.full_name.split("/", 1)
            branch = repo.sync_branch or repo.default_branch or "main"

            try:
                tree = await self.github_service.get_repo_tree(owner, repo_name, branch)
                sha_by_path = {item.path: item.sha for item in tree.all_items}

                for doc in repo_docs:
                    if not doc.github_path:
                        continue

                    remote_sha = sha_by_path.get(doc.github_path)

                    if remote_sha is None:
                        # File deleted from remote
                        statuses.append(
                            DocumentSyncStatus(
                                document_id=str(doc.id),
                                status="remote_changes",
                                local_sha=doc.github_sha,
                                remote_sha=None,
                            )
                        )
                    elif remote_sha != doc.github_sha:
                        # File changed on remote
                        statuses.append(
                            DocumentSyncStatus(
                                document_id=str(doc.id),
                                status="remote_changes",
                                local_sha=doc.github_sha,
                                remote_sha=remote_sha,
                            )
                        )
                    else:
                        # Check for local changes (updated_at > last_synced_at)
                        updated = _ensure_aware(doc.updated_at)
                        synced = _ensure_aware(doc.last_synced_at)
                        if synced and updated and updated > synced:
                            statuses.append(
                                DocumentSyncStatus(
                                    document_id=str(doc.id),
                                    status="local_changes",
                                    local_sha=doc.github_sha,
                                    remote_sha=remote_sha,
                                )
                            )
                        else:
                            statuses.append(
                                DocumentSyncStatus(
                                    document_id=str(doc.id),
                                    status="synced",
                                    local_sha=doc.github_sha,
                                    remote_sha=remote_sha,
                                )
                            )

            except GitHubAPIError as e:
                for doc in repo_docs:
                    statuses.append(
                        DocumentSyncStatus(
                            document_id=str(doc.id),
                            status="error",
                            error=str(e),
                        )
                    )

        return statuses

    async def pull_remote_changes(
        self,
        document_id: str,
    ) -> Document | None:
        """
        Pull latest content from GitHub for a document.

        Updates the document with remote content and resets sync status.
        RLS enforces product access.

        Args:
            document_id: Document to update

        Returns:
            Updated Document or None if not found
        """
        result = await self.db.execute(
            select(Document).where(Document.id == uuid_pkg.UUID(document_id))
        )
        doc = result.scalar_one_or_none()

        if not doc or not doc.github_path or not doc.repository_id:
            return None

        # Get repository
        repo_result = await self.db.execute(
            select(Repository).where(Repository.id == uuid_pkg.UUID(str(doc.repository_id)))
        )
        repo = repo_result.scalar_one_or_none()

        if not repo or not repo.full_name:
            return None

        owner, repo_name = repo.full_name.split("/", 1)
        branch = repo.sync_branch or repo.default_branch or "main"

        try:
            file_content = await self.github_service.get_file_content(
                owner, repo_name, doc.github_path, branch
            )
            if not file_content:
                return None

            # Update document
            doc.content = file_content.content
            doc.github_sha = file_content.sha
            doc.last_synced_at = datetime.now(UTC)
            doc.sync_status = "synced"
            doc.title = extract_title(file_content.content, doc.github_path)

            await self.db.commit()
            # Commit dropped SET LOCAL; re-arm before the refresh SELECT.
            await set_rls_user_context(self.db, self.user_id)
            await self.db.refresh(doc)
            return doc

        except GitHubAPIError as e:
            logger.error(f"Failed to pull remote changes: {e}")
            return None

    def _resolve_doc_path(self, doc: Document, path_prefix: str) -> str:
        """
        Resolve the file path for a document in the target repository.

        Uses the existing github_path if set (preserves round-trip paths),
        otherwise generates a path using the sync path prefix and the
        document's folder/title.

        Args:
            doc: The document to resolve the path for
            path_prefix: Sync path prefix (e.g. "docs/", ".trajan/")

        Returns:
            File path relative to repository root
        """
        if doc.github_path:
            return doc.github_path

        # Build path: {prefix}{folder}/{slug}.md
        # generate_github_path hardcodes "docs/" — replace with the configured prefix
        default_path = generate_github_path(
            doc.title or "untitled",
            doc.folder.get("path") if doc.folder else None,
            doc.type or "blueprint",
        )
        if path_prefix != "docs/":
            # Strip the default "docs/" and prepend the configured prefix
            prefix = path_prefix.rstrip("/") + "/"
            if default_path.startswith("docs/"):
                return prefix + default_path[len("docs/") :]
            return prefix + default_path
        return default_path

    @staticmethod
    def _build_pr_body(files: list[dict[str, str]]) -> str:
        """Build a PR description listing the synced files."""
        lines = ["## Synced from Trajan", ""]
        lines.append(f"This PR syncs **{len(files)}** document(s):")
        lines.append("")
        for f in files:
            lines.append(f"- `{f['path']}`")
        lines.append("")
        lines.append("*Auto-generated by Trajan doc sync.*")
        return "\n".join(lines)

    def _is_doc_file(self, item: RepoTreeItem) -> bool:
        """Check if tree item is a documentation file we should import."""
        if item.type != "blob":
            return False
        if not item.path.endswith(".md"):
            return False

        path_lower = item.path.lower()

        # Include docs/ folder
        if item.path.startswith("docs/"):
            return True

        # Include root-level changelog files
        return path_lower in ("changelog.md", "changes.md", "history.md")

    async def _find_by_github_path(
        self,
        product_id: uuid_pkg.UUID | None,
        github_path: str,
    ) -> Document | None:
        """Find document by GitHub path."""
        if product_id is None:
            return None
        result = await self.db.execute(
            select(Document)
            .where(Document.product_id == product_id)
            .where(Document.github_path == github_path)
        )
        return result.scalar_one_or_none()

    async def _create_from_github(
        self,
        repository: Repository,
        item: RepoTreeItem,
        branch: str,
    ) -> Document:
        """Create a new document from GitHub file."""
        if not repository.full_name:
            raise ValueError("Repository has no full_name")

        owner, repo_name = repository.full_name.split("/", 1)

        file_content = await self.github_service.get_file_content(
            owner, repo_name, item.path, branch
        )
        if not file_content:
            raise ValueError(f"Could not fetch content for {item.path}")

        content = file_content.content
        folder_path = map_path_to_folder(item.path)
        doc_type = infer_doc_type(item.path, content)

        doc = Document(
            product_id=repository.product_id,
            created_by_user_id=repository.imported_by_user_id,
            title=extract_title(content, item.path),
            content=content,
            type=doc_type,
            folder={"path": folder_path} if folder_path else None,
            repository_id=repository.id,
            # Sync tracking
            github_sha=item.sha,
            github_path=item.path,
            last_synced_at=datetime.now(UTC),
            sync_status="synced",
        )
        self.db.add(doc)
        return doc

    async def _update_from_github(
        self,
        document: Document,
        repository: Repository,
        item: RepoTreeItem,
        branch: str,
    ) -> None:
        """Update existing document from GitHub."""
        if not repository.full_name:
            return

        owner, repo_name = repository.full_name.split("/", 1)

        file_content = await self.github_service.get_file_content(
            owner, repo_name, item.path, branch
        )
        if not file_content:
            return

        document.content = file_content.content
        document.github_sha = file_content.sha
        document.last_synced_at = datetime.now(UTC)
        document.sync_status = "synced"
        document.title = extract_title(file_content.content, item.path)
