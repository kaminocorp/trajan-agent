"""Document refresh endpoints — AI-powered document updates."""

import uuid as uuid_pkg

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db_with_rls
from app.domain import document_ops, product_ops, repository_ops
from app.models.user import User
from app.schemas.docs import (
    BulkRefreshResponse,
    RefreshDocumentDetailResponse,
    RefreshDocumentResponse,
)
from app.services.docs.document_refresher import DocumentRefresher
from app.services.github import GitHubService
from app.services.github.token_resolver import TokenResolver


async def refresh_document(
    document_id: uuid_pkg.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> RefreshDocumentResponse:
    """
    Refresh a single document by comparing with current codebase.

    Reviews the document against the current state of the source files
    and updates if any information is outdated. RLS enforces product access.
    """
    doc = await document_ops.get(db, id=document_id)
    if not doc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Document not found",
        )

    if not doc.product_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Document is not linked to a product",
        )

    # Get linked repositories (RLS enforces product access)
    repos = await repository_ops.get_github_repos_by_product(db, product_id=doc.product_id)
    if not repos:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No GitHub repositories linked to this product",
        )

    # Resolve token — prefer the document's repo if linked, else primary
    resolver = TokenResolver(db)
    token_repo = next((r for r in repos if r.id == doc.repository_id), repos[0])
    github_token, _ = await resolver.resolve_token(token_repo, current_user.id)
    if not github_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No GitHub token available. Connect a GitHub App, add a per-repo token, "
            "or configure a personal access token in Settings.",
        )

    github_service = GitHubService(github_token)
    refresher = DocumentRefresher(db, github_service)

    result = await refresher.refresh_document(doc, repos)

    return RefreshDocumentResponse(
        document_id=result.document_id,
        status=result.status,
        changes_summary=result.changes_summary,
        error=result.error,
    )


async def refresh_all_documents(
    product_id: uuid_pkg.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db_with_rls),
) -> BulkRefreshResponse:
    """
    Refresh all documents for a product.

    Scans all documents and compares them against the current state
    of the codebase. Updates any documents that have become outdated.
    RLS enforces product access.
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

    # Resolve token via primary repo (per-repo > GitHub App > PAT)
    resolver = TokenResolver(db)
    github_token, _ = await resolver.resolve_token(repos[0], current_user.id)
    if not github_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No GitHub token available. Connect a GitHub App, add a per-repo token, "
            "or configure a personal access token in Settings.",
        )

    github_service = GitHubService(github_token)
    refresher = DocumentRefresher(db, github_service)

    result = await refresher.refresh_all(
        product_id=str(product_id),
        repos=repos,
    )

    return BulkRefreshResponse(
        checked=result.checked,
        updated=result.updated,
        unchanged=result.unchanged,
        errors=result.errors,
        details=[
            RefreshDocumentDetailResponse(
                document_id=d.document_id,
                status=d.status,
                changes_summary=d.changes_summary,
                error=d.error,
            )
            for d in result.details
        ],
    )
