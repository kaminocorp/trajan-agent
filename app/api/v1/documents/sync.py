"""GitHub synchronization endpoints for documents."""

import uuid as uuid_pkg

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_with_rls
from app.api.v1.documents.crud import serialize_document
from app.domain import document_ops, product_ops, repository_ops
from app.models.user import User
from app.schemas.docs import (
    DocsSyncStatusResponse,
    DocumentSyncStatusResponse,
    ImportDocsResponse,
    SyncConfigResponse,
    SyncConfigUpdate,
    SyncDocsRequest,
    SyncDocsResponse,
)
from app.services.docs.sync_service import DocsSyncService
from app.services.github import GitHubService
from app.services.github.token_resolver import TokenResolver


async def import_docs_from_repo(
    product_id: uuid_pkg.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> ImportDocsResponse:
    """
    Import documentation from linked GitHub repositories.

    Scans all repositories linked to the product for docs/ folder
    and imports markdown files with sync tracking. RLS enforces product access.
    """
    product = await product_ops.get(db, id=product_id)
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found",
        )

    # Get linked repositories (RLS enforces product access)
    repos = await repository_ops.get_github_repos_by_product(db, product_id=product_id)
    if not repos:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No GitHub repositories linked to this product",
        )

    resolver = TokenResolver(db)
    total_imported = 0
    total_updated = 0
    total_skipped = 0

    for repo in repos:
        if not repo.full_name:
            continue
        github_token, _ = await resolver.resolve_token(repo, current_user.id)
        if not github_token:
            continue
        github_service = GitHubService(github_token)
        sync_service = DocsSyncService(db, github_service, user_id=current_user.id)
        result = await sync_service.import_from_repo(repo)
        total_imported += result.imported
        total_updated += result.updated
        total_skipped += result.skipped

    if total_imported == 0 and total_updated == 0 and total_skipped == 0 and repos:
        # Check if the failure was due to missing tokens
        any_token, _ = await resolver.resolve_token(repos[0], current_user.id)
        if not any_token:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No GitHub token available. Connect a GitHub App, add a per-repo token, "
                "or configure a personal access token in Settings.",
            )

    return ImportDocsResponse(
        imported=total_imported,
        updated=total_updated,
        skipped=total_skipped,
    )


async def get_docs_sync_status(
    product_id: uuid_pkg.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> DocsSyncStatusResponse:
    """
    Check sync status for all documents in a product.

    Returns which documents have local changes, remote changes,
    or are in sync with GitHub. RLS enforces product access.
    """
    product = await product_ops.get(db, id=product_id)
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found",
        )

    # Resolve token via primary repo (status check covers all synced docs)
    repos = await repository_ops.get_github_repos_by_product(db, product_id=product_id)
    resolver = TokenResolver(db)
    github_token: str | None = None
    if repos:
        github_token, _ = await resolver.resolve_token(repos[0], current_user.id)

    if not github_token:
        # Return empty status if no token available
        return DocsSyncStatusResponse(
            documents=[],
            has_local_changes=False,
            has_remote_changes=False,
        )

    github_service = GitHubService(github_token)
    sync_service = DocsSyncService(db, github_service, user_id=current_user.id)

    statuses = await sync_service.check_for_updates(product_id=str(product_id))

    return DocsSyncStatusResponse(
        documents=[
            DocumentSyncStatusResponse(
                document_id=s.document_id,
                status=s.status,
                local_sha=s.local_sha,
                remote_sha=s.remote_sha,
                error=s.error,
            )
            for s in statuses
        ],
        has_local_changes=any(s.status == "local_changes" for s in statuses),
        has_remote_changes=any(s.status == "remote_changes" for s in statuses),
    )


async def pull_remote_changes(
    document_id: uuid_pkg.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
):
    """
    Pull latest content from GitHub for a document.

    Overwrites local content with remote version. RLS enforces product access.
    """
    doc = await document_ops.get(db, id=document_id)
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found",
        )

    if not doc.github_path:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Document is not synced with GitHub",
        )

    # Resolve token via the document's repository
    github_token: str | None = None
    if doc.repository_id:
        repo = await repository_ops.get(db, id=doc.repository_id)
        if repo:
            resolver = TokenResolver(db)
            github_token, _ = await resolver.resolve_token(repo, current_user.id)
    if not github_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No GitHub token available. Connect a GitHub App, add a per-repo token, "
            "or configure a personal access token in Settings.",
        )

    github_service = GitHubService(github_token)
    sync_service = DocsSyncService(db, github_service, user_id=current_user.id)

    updated_doc = await sync_service.pull_remote_changes(document_id=str(document_id))

    if not updated_doc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to pull remote changes",
        )

    return serialize_document(updated_doc)


async def sync_docs_to_repo(
    product_id: uuid_pkg.UUID,
    data: SyncDocsRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> SyncDocsResponse:
    """
    Push documentation to linked GitHub repository.

    Syncs specified documents (or all with local changes) to the
    repository's configured branch. Uses TokenResolver to select
    the best token (per-repo > GitHub App > PAT). RLS enforces
    product access.
    """
    product = await product_ops.get(db, id=product_id)
    if not product:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Product not found",
        )

    # Get documents to sync
    if data.document_ids:
        documents = []
        for doc_id in data.document_ids:
            doc = await document_ops.get(db, id=uuid_pkg.UUID(doc_id))
            if doc:
                documents.append(doc)
    else:
        # Sync all with local changes
        documents = await document_ops.get_with_local_changes(db, product_id=product_id)

    if not documents:
        return SyncDocsResponse(
            success=True,
            files_synced=0,
            errors=["No documents to sync"],
        )

    # Get primary repository for this product (RLS enforces access)
    repos = await repository_ops.get_github_repos_by_product(db, product_id=product_id)
    if not repos:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No GitHub repositories linked to this product",
        )

    # Use first repo (primary)
    repo = repos[0]

    # Enforce sync_enabled flag
    if not repo.sync_enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Sync is not enabled for this repository. "
            "Enable it in Sync Settings before pushing.",
        )

    # Resolve token via TokenResolver (per-repo > GitHub App > PAT)
    resolver = TokenResolver(db)
    github_token, token_method = await resolver.resolve_token(repo, current_user.id)
    if not github_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No GitHub token available. Connect a GitHub App, add a per-repo token, "
            "or configure a personal access token in Settings.",
        )

    # Warn if token may lack write permission (App tokens with read-only contents)
    if token_method == "github_app" and product.organization_id:
        from app.domain import github_app_installation_ops

        installation = await github_app_installation_ops.get_for_org(db, product.organization_id)
        if installation and installation.permissions.get("contents") == "read":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="The GitHub App installation has read-only 'contents' permission. "
                "Sync requires 'contents: write'. Please update the App permissions "
                "in your GitHub organization settings.",
            )

    github_service = GitHubService(github_token)
    sync_service = DocsSyncService(db, github_service, user_id=current_user.id)

    result = await sync_service.sync_to_repo(documents, repo, data.message)

    return SyncDocsResponse(
        success=result.success,
        files_synced=result.files_synced,
        commit_sha=result.commit_sha,
        branch=result.branch,
        pr_url=result.pr_url,
        pr_number=result.pr_number,
        errors=result.errors,
    )


async def get_sync_config(
    repository_id: uuid_pkg.UUID,
    _current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> SyncConfigResponse:
    """
    Get sync configuration for a repository.

    Returns the current sync settings (branch, path prefix, PR mode, doc filter).
    RLS enforces product access via the repository's product.
    """
    repo = await repository_ops.get(db, id=repository_id)
    if not repo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repository not found",
        )

    return SyncConfigResponse(
        sync_enabled=repo.sync_enabled,
        sync_branch=repo.sync_branch,
        sync_path_prefix=repo.sync_path_prefix,
        sync_create_pr=repo.sync_create_pr,
        sync_doc_filter=repo.sync_doc_filter,
        last_sync_commit_sha=repo.last_sync_commit_sha,
        last_sync_pr_url=repo.last_sync_pr_url,
    )


async def update_sync_config(
    repository_id: uuid_pkg.UUID,
    data: SyncConfigUpdate,
    _current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> SyncConfigResponse:
    """
    Update sync configuration for a repository.

    Allows setting target branch, path prefix, PR mode, and document filter.
    Only provided fields are updated (partial update). RLS enforces product access.
    """
    repo = await repository_ops.get(db, id=repository_id)
    if not repo:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Repository not found",
        )

    # Apply only provided fields
    update_data = data.model_dump(exclude_unset=True)
    for field_name, value in update_data.items():
        setattr(repo, field_name, value)

    await db.commit()
    await db.refresh(repo)

    return SyncConfigResponse(
        sync_enabled=repo.sync_enabled,
        sync_branch=repo.sync_branch,
        sync_path_prefix=repo.sync_path_prefix,
        sync_create_pr=repo.sync_create_pr,
        sync_doc_filter=repo.sync_doc_filter,
        last_sync_commit_sha=repo.last_sync_commit_sha,
        last_sync_pr_url=repo.last_sync_pr_url,
    )
