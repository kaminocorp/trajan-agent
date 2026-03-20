"""Repository documentation scanning endpoints.

Provides read-only access to documentation files from linked GitHub repositories.
"""

import uuid as uuid_pkg
from datetime import UTC, datetime

from fastapi import Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_with_rls
from app.domain import product_ops, repository_ops
from app.domain.organization_operations import organization_ops
from app.domain.product_access_operations import product_access_ops
from app.models.user import User
from app.schemas.repo_docs import (
    RepoDocContent,
    RepoDocDirectory,
    RepoDocFile,
    RepoDocsTree,
    RepoDocsTreeResponse,
)
from app.services.github import GitHubService
from app.services.github.constants import is_documentation_file
from app.services.github.token_resolver import TokenResolver


def _build_doc_tree(
    files: list[tuple[str, int, str]],  # (path, size, sha)
) -> tuple[list[RepoDocFile], list[RepoDocDirectory]]:
    """
    Build a hierarchical tree structure from flat file list.

    Returns root-level files and directories with full nested structure.
    """
    root_files: list[RepoDocFile] = []
    # Use nested dict structure: path -> RepoDocDirectory
    directories: dict[str, RepoDocDirectory] = {}

    def get_or_create_directory(dir_parts: list[str]) -> RepoDocDirectory:
        """Recursively get or create directory structure."""
        full_path = "/".join(dir_parts)

        if full_path in directories:
            return directories[full_path]

        # Create this directory
        directory = RepoDocDirectory(
            path=full_path,
            name=dir_parts[-1],
            files=[],
            directories=[],
        )
        directories[full_path] = directory

        # If this is nested, ensure parent exists and link to it
        if len(dir_parts) > 1:
            parent = get_or_create_directory(dir_parts[:-1])
            # Add this directory to parent if not already there
            if directory not in parent.directories:
                parent.directories.append(directory)

        return directory

    for path, size, sha in files:
        parts = path.split("/")
        filename = parts[-1]

        if len(parts) == 1:
            # Root-level file
            root_files.append(
                RepoDocFile(
                    path=path,
                    name=filename,
                    size=size,
                    sha=sha,
                )
            )
        else:
            # File in a directory - build full nested structure
            dir_parts = parts[:-1]
            directory = get_or_create_directory(dir_parts)

            # Add file to its immediate parent directory
            directory.files.append(
                RepoDocFile(
                    path=path,
                    name=filename,
                    size=size,
                    sha=sha,
                )
            )

    # Return only top-level directories (those with single-part paths)
    top_level_dirs = [d for path, d in directories.items() if "/" not in path]

    return root_files, top_level_dirs


async def get_repo_docs_tree(
    product_id: uuid_pkg.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> RepoDocsTreeResponse:
    """
    Scan all linked repositories for documentation files.

    Returns a tree structure of all documentation files found in each
    linked repository. Documentation files are identified by:
    - File extension (.md, .mdx, .rst)
    - Location (docs/, documentation/, doc/ directories)
    - Known names (README, CHANGELOG, CONTRIBUTING, etc.)
    """
    product = await product_ops.get(db, product_id)
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found",
        )

    # Check org membership and product access (at least viewer)
    if not product.organization_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found",
        )

    org_role = await organization_ops.get_member_role(db, product.organization_id, current_user.id)
    if not org_role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found",
        )

    access = await product_access_ops.get_effective_access(
        db, product_id, current_user.id, org_role
    )
    if access == "none":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found",
        )

    # Get linked repositories (RLS enforces product access)
    repos = await repository_ops.get_github_repos_by_product(db, product_id=product_id)

    if not repos:
        return RepoDocsTreeResponse(
            repositories=[],
            total_files=0,
            fetched_at=datetime.now(UTC),
        )

    # Use TokenResolver to resolve the best token per-repo (per-repo > App > PAT)
    resolver = TokenResolver(db)

    repo_trees: list[RepoDocsTree] = []
    total_files = 0

    for repo in repos:
        if not repo.full_name:
            continue

        try:
            # Resolve token for this specific repo
            token, _method = await resolver.resolve_token(repo, current_user.id)
            if not token:
                continue

            owner, repo_name = repo.full_name.split("/")
            branch = repo.default_branch or "main"
            github_service = GitHubService(token)

            # Fetch repository tree
            tree = await github_service.get_repo_tree(owner, repo_name, branch)

            # Filter for documentation files
            doc_files: list[tuple[str, int, str]] = []
            for item in tree.all_items:
                if item.type == "blob" and is_documentation_file(item.path):
                    doc_files.append((item.path, item.size or 0, item.sha))

            # Build tree structure
            root_files, directories = _build_doc_tree(doc_files)

            repo_trees.append(
                RepoDocsTree(
                    repository_id=str(repo.id),
                    repository_name=repo.full_name,
                    branch=branch,
                    files=root_files,
                    directories=directories,
                )
            )

            total_files += len(doc_files)

        except Exception:
            # Skip repos that fail (private, deleted, etc.)
            # Could log this for debugging
            continue

    return RepoDocsTreeResponse(
        repositories=repo_trees,
        total_files=total_files,
        fetched_at=datetime.now(UTC),
    )


async def get_repo_file_content(
    repository_id: uuid_pkg.UUID,
    path: str = Query(..., description="File path within the repository"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> RepoDocContent:
    """
    Fetch the content of a specific file from a repository.

    Returns the file content as text. Large files (>100KB) will be truncated.
    """
    repo = await repository_ops.get(db, id=repository_id)
    if not repo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repository not found",
        )

    if not repo.full_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Repository has no GitHub link",
        )

    # Resolve token for this specific repo (per-repo > App > PAT)
    resolver = TokenResolver(db)
    token, _method = await resolver.resolve_token(repo, current_user.id)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No GitHub access for this repository. Install the GitHub App, "
            "add a Personal Access Token, or link the repo with a fine-grained token.",
        )

    github_service = GitHubService(token)

    try:
        owner, repo_name = repo.full_name.split("/")
        branch = repo.default_branch or "main"

        # Try to fetch file content (with larger limit for docs)
        max_size = 500_000  # 500KB for docs
        file_content = await github_service.get_file_content(
            owner, repo_name, path, branch, max_size
        )

        if not file_content:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="File not found or is binary/too large",
            )

        # Check if we should truncate for very large files
        truncated = False
        content = file_content.content
        if len(content) > 100_000:
            content = content[:100_000] + "\n\n... [Content truncated - file too large]"
            truncated = True

        return RepoDocContent(
            path=path,
            content=content,
            size=file_content.size,
            sha=file_content.sha,
            repository_id=str(repo.id),
            repository_name=repo.full_name,
            branch=branch,
            truncated=truncated,
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to fetch file: {str(e)}",
        ) from e
