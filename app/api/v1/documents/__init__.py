"""Documents API package — split for maintainability.

Modules:
- crud.py — Basic CRUD operations + product-scoped endpoints
- lifecycle.py — Plan state transitions (move-to-executing, move-to-completed, archive)
- sync.py — GitHub synchronization endpoints
- refresh.py — Document refresh endpoints
- repo_docs.py — Repository documentation scanning endpoints
- sections.py — Document sections management (custom sections, reordering)
"""

from fastapi import APIRouter

# Import route handlers from sub-modules
from app.api.v1.documents.crud import (
    add_changelog_entry,
    create_document,
    delete_all_generated_documents,
    delete_document,
    get_document,
    get_documents_grouped,
    list_documents,
    serialize_document,
    update_document,
)
from app.api.v1.documents.custom import (
    cancel_custom_doc_job,
    generate_assessment,
    generate_custom_document,
    get_custom_doc_status,
)
from app.api.v1.documents.lifecycle import (
    archive_document,
    move_to_completed,
    move_to_executing,
)
from app.api.v1.documents.refresh import (
    refresh_all_documents,
    refresh_document,
)
from app.api.v1.documents.repo_docs import (
    get_repo_docs_tree,
    get_repo_file_content,
)
from app.api.v1.documents.sections import (
    create_section,
    create_subsection,
    delete_section,
    delete_subsection,
    list_sections,
    move_document_to_section,
    reorder_sections,
    reorder_subsections,
    update_section,
    update_subsection,
)
from app.api.v1.documents.sync import (
    get_docs_sync_status,
    get_sync_config,
    import_docs_from_repo,
    pull_remote_changes,
    sync_docs_to_repo,
    update_sync_config,
)

router = APIRouter(prefix="/documents", tags=["documents"])

# CRUD routes
router.add_api_route("", list_documents, methods=["GET"], response_model=list[dict])
router.add_api_route("/{document_id}", get_document, methods=["GET"])
router.add_api_route("", create_document, methods=["POST"], status_code=201)
router.add_api_route("/{document_id}", update_document, methods=["PATCH"])
router.add_api_route("/{document_id}", delete_document, methods=["DELETE"], status_code=204)

# Product-scoped routes
router.add_api_route("/products/{product_id}/grouped", get_documents_grouped, methods=["GET"])
router.add_api_route(
    "/products/{product_id}/changelog/add-entry", add_changelog_entry, methods=["POST"]
)
router.add_api_route(
    "/products/{product_id}/generated",
    delete_all_generated_documents,
    methods=["DELETE"],
    status_code=200,
)

# Lifecycle routes
router.add_api_route("/{document_id}/move-to-executing", move_to_executing, methods=["POST"])
router.add_api_route("/{document_id}/move-to-completed", move_to_completed, methods=["POST"])
router.add_api_route("/{document_id}/archive", archive_document, methods=["POST"])

# Sync routes
router.add_api_route("/products/{product_id}/import-docs", import_docs_from_repo, methods=["POST"])
router.add_api_route(
    "/products/{product_id}/docs-sync-status", get_docs_sync_status, methods=["GET"]
)
router.add_api_route("/{document_id}/pull-remote", pull_remote_changes, methods=["POST"])
router.add_api_route("/products/{product_id}/sync-docs", sync_docs_to_repo, methods=["POST"])
router.add_api_route("/repositories/{repository_id}/sync-config", get_sync_config, methods=["GET"])
router.add_api_route(
    "/repositories/{repository_id}/sync-config", update_sync_config, methods=["PATCH"]
)

# Refresh routes
router.add_api_route("/{document_id}/refresh", refresh_document, methods=["POST"])
router.add_api_route(
    "/products/{product_id}/refresh-all-docs", refresh_all_documents, methods=["POST"]
)

# Repository docs routes (read-only scanning)
router.add_api_route("/products/{product_id}/repo-docs-tree", get_repo_docs_tree, methods=["GET"])
router.add_api_route(
    "/repositories/{repository_id}/file-content", get_repo_file_content, methods=["GET"]
)

# Custom document generation routes
router.add_api_route(
    "/products/{product_id}/custom/generate",
    generate_custom_document,
    methods=["POST"],
)
router.add_api_route(
    "/products/{product_id}/custom/status/{job_id}",
    get_custom_doc_status,
    methods=["GET"],
)
router.add_api_route(
    "/products/{product_id}/custom/cancel/{job_id}",
    cancel_custom_doc_job,
    methods=["DELETE"],
)

# Assessment generation route
router.add_api_route(
    "/products/{product_id}/assessment/{assessment_type}/generate",
    generate_assessment,
    methods=["POST"],
)

# Section management routes
router.add_api_route("/products/{product_id}/sections", list_sections, methods=["GET"])
router.add_api_route(
    "/products/{product_id}/sections", create_section, methods=["POST"], status_code=201
)
router.add_api_route("/sections/{section_id}", update_section, methods=["PATCH"])
router.add_api_route("/sections/{section_id}", delete_section, methods=["DELETE"], status_code=204)
router.add_api_route("/products/{product_id}/sections/reorder", reorder_sections, methods=["PATCH"])

# Subsection routes
router.add_api_route(
    "/sections/{section_id}/subsections", create_subsection, methods=["POST"], status_code=201
)
router.add_api_route("/subsections/{subsection_id}", update_subsection, methods=["PATCH"])
router.add_api_route(
    "/subsections/{subsection_id}", delete_subsection, methods=["DELETE"], status_code=204
)
router.add_api_route(
    "/sections/{section_id}/subsections/reorder", reorder_subsections, methods=["PATCH"]
)

# Document section movement
router.add_api_route("/{document_id}/move-to-section", move_document_to_section, methods=["PATCH"])

__all__ = ["router", "serialize_document"]
