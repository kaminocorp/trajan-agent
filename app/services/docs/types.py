"""
Shared data types for Documentation Agent services.

These dataclasses are used across DocumentOrchestrator and sub-agents
(ChangelogAgent, BlueprintAgent, PlansAgent).
"""

from dataclasses import dataclass, field

from app.models.document import Document
from app.services.github.types import RepoTreeItem


@dataclass
class DocsInfo:
    """Information about existing documentation in a repository."""

    has_docs_folder: bool
    has_markdown_files: bool
    files: list[RepoTreeItem]


@dataclass
class ChangelogResult:
    """Result of ChangelogAgent processing."""

    action: str  # "found_existing", "created", "updated"
    document: Document


@dataclass
class ChangeEntry:
    """A single entry for changelog updates."""

    category: str  # "Added", "Changed", "Fixed", "Removed", etc.
    description: str


@dataclass
class DocumentSpec:
    """Specification for a document to be generated."""

    title: str
    folder_path: str
    doc_type: str
    prompt_context: str


@dataclass
class BlueprintPlan:
    """Plan for which blueprint documents need to be created."""

    documents_to_create: list[DocumentSpec] = field(default_factory=list)


@dataclass
class BlueprintResult:
    """Result of BlueprintAgent processing."""

    documents: list[Document] = field(default_factory=list)
    created_count: int = 0


@dataclass
class PlansResult:
    """Result of PlansAgent processing."""

    organized_count: int = 0


@dataclass
class OrchestratorResult:
    """Complete result of DocumentOrchestrator processing."""

    imported: list[Document] = field(default_factory=list)
    changelog: ChangelogResult | None = None
    blueprints: list[Document] = field(default_factory=list)
    plans_structured: PlansResult | None = None


# ─────────────────────────────────────────────────────────────
# Phase 2: GitHub Synchronization Types
# ─────────────────────────────────────────────────────────────


@dataclass
class ImportResult:
    """Result of importing documents from GitHub."""

    imported: int = 0  # New documents created
    updated: int = 0  # Existing documents updated
    skipped: int = 0  # Unchanged documents skipped


@dataclass
class SyncResult:
    """Result of syncing documents to GitHub."""

    success: bool
    files_synced: int = 0
    errors: list[str] = field(default_factory=list)
    commit_sha: str | None = None
    branch: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None


@dataclass
class DocumentSyncStatus:
    """Sync status for a single document."""

    document_id: str
    status: str  # "synced", "local_changes", "remote_changes", "conflict", "error"
    local_sha: str | None = None
    remote_sha: str | None = None
    error: str | None = None


# ─────────────────────────────────────────────────────────────
# Documentation Agent v2: Codebase Analysis Types
# ─────────────────────────────────────────────────────────────


@dataclass
class FileContent:
    """Content of a file with metadata."""

    path: str
    content: str
    size: int
    tier: int  # Priority tier (1=highest, 3=lowest)
    token_estimate: int  # Estimated token count


@dataclass
class TechStack:
    """Detected technology stack for a codebase."""

    languages: list[str]  # Primary languages (e.g., ["Python", "TypeScript"])
    frameworks: list[str]  # Detected frameworks (e.g., ["FastAPI", "Next.js"])
    databases: list[str]  # Detected databases (e.g., ["PostgreSQL", "Redis"])
    infrastructure: list[str]  # Infra tools (e.g., ["Docker", "Fly.io"])
    package_managers: list[str]  # Package managers (e.g., ["pip", "npm"])


@dataclass
class ModelInfo:
    """Information about a detected data model/schema."""

    name: str
    file_path: str
    model_type: str  # "sqlmodel", "pydantic", "typescript", "prisma", etc.
    fields: list[str]  # Field names (summary)


@dataclass
class EndpointInfo:
    """Information about a detected API endpoint."""

    method: str  # GET, POST, PUT, DELETE, etc.
    path: str  # Route path
    file_path: str  # Source file
    handler_name: str | None  # Function/method name


@dataclass
class RepoAnalysis:
    """Analysis results for a single repository."""

    full_name: str
    default_branch: str
    description: str | None
    tech_stack: TechStack
    key_files: list[FileContent]
    models: list[ModelInfo]
    endpoints: list[EndpointInfo]
    detected_patterns: list[str]  # "REST API", "monorepo", "microservices", etc.
    total_files: int
    errors: list[str] = field(default_factory=list)


@dataclass
class CodebaseContext:
    """
    Complete codebase context for documentation planning.

    This is the output of CodebaseAnalyzer and input to DocumentationPlanner.
    Contains deep analysis of all repositories linked to a product.
    """

    repositories: list[RepoAnalysis]
    combined_tech_stack: TechStack  # Merged across all repos
    all_key_files: list[FileContent]  # Files from all repos
    all_models: list[ModelInfo]  # Models from all repos
    all_endpoints: list[EndpointInfo]  # Endpoints from all repos
    detected_patterns: list[str]  # Overall patterns
    total_files: int
    total_tokens: int  # Token count of all file contents
    errors: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────
# Documentation Agent v2: Planning Types
# ─────────────────────────────────────────────────────────────


@dataclass
class PlannedDocument:
    """
    A document that the planner has decided should be created.

    This is the output of DocumentationPlanner, describing what documentation
    should exist and providing guidance for the generator.
    """

    title: str
    doc_type: str  # "overview", "architecture", "guide", "reference", "concept"
    purpose: str  # Why this doc is valuable, who it serves
    key_topics: list[str]  # What should be covered
    source_files: list[str]  # File paths to reference when generating
    priority: int  # 1-5 (1 = most important)
    folder: str = "blueprints"  # Target folder
    section: str = "technical"  # "technical" or "conceptual"
    subsection: str = "overview"  # e.g., "backend", "frontend", "concepts"


@dataclass
class DocumentationPlan:
    """
    Complete documentation plan created by DocumentationPlanner.

    Contains the planner's assessment of the codebase and an ordered list
    of documents to generate. Also tracks which existing docs already cover
    certain areas (to avoid duplication).
    """

    summary: str  # High-level assessment of the codebase
    planned_documents: list[PlannedDocument]  # Ordered by priority
    skipped_existing: list[str]  # Titles of existing docs that already cover areas
    codebase_summary: str  # Brief summary of tech stack and architecture


@dataclass
class PlannerResult:
    """Result of DocumentationPlanner processing."""

    plan: DocumentationPlan
    success: bool = True
    error: str | None = None


# ─────────────────────────────────────────────────────────────
# Documentation Agent v2: Generation Types
# ─────────────────────────────────────────────────────────────


@dataclass
class GeneratorResult:
    """Result of DocumentGenerator processing a single document."""

    document: Document | None
    success: bool = True
    error: str | None = None


@dataclass
class BatchGeneratorResult:
    """Result of generating multiple documents from a plan."""

    documents: list[Document] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)  # Titles of docs that failed
    total_planned: int = 0
    total_generated: int = 0


# ─────────────────────────────────────────────────────────────
# Custom Documentation Request Types
# ─────────────────────────────────────────────────────────────


@dataclass
class CustomDocRequest:
    """User's request for custom documentation."""

    prompt: str  # Free-form user prompt (required)
    doc_type: str  # "how-to" | "wiki" | "overview" | "technical" | "guide"
    format_style: str  # "technical" | "presentation" | "essay" | "email" | "how-to-guide"
    target_audience: str  # "internal-technical" | "internal-non-technical" | etc.
    focus_paths: list[str] | None = None  # Optional: specific files/folders to focus on
    title: str | None = None  # Optional: user-provided title (auto-generated if not provided)


@dataclass
class ValidationWarning:
    """Warning about a potential hallucination in generated content."""

    claim_type: str  # "endpoint", "model", "technology"
    claim: str  # The specific claim (e.g., "/api/v1/payments", "PaymentModel")
    message: str  # Human-readable warning message
    severity: str = "medium"  # "low", "medium", "high"


@dataclass
class ExtractedClaims:
    """Claims extracted from generated documentation for validation."""

    endpoints: list[str]  # API endpoints mentioned (e.g., "/api/v1/users")
    models: list[str]  # Model/class names mentioned (e.g., "User", "Product")
    technologies: list[str]  # Technologies mentioned (e.g., "Redis", "PostgreSQL")


@dataclass
class ValidationResult:
    """Result of validating generated content against codebase."""

    warnings: list[ValidationWarning]
    claims_checked: int
    claims_verified: int

    @property
    def has_warnings(self) -> bool:
        return len(self.warnings) > 0

    @property
    def confidence_score(self) -> float:
        """Score from 0-1 indicating how well claims match codebase."""
        if self.claims_checked == 0:
            return 1.0  # No claims to verify = no issues
        return self.claims_verified / self.claims_checked


@dataclass
class CustomDocResult:
    """Result of custom document generation."""

    success: bool
    content: str | None = None  # Raw markdown content
    suggested_title: str | None = None  # AI-suggested title if user didn't provide one
    document: Document | None = None  # The saved document (if saved)
    error: str | None = None
    generation_time_seconds: float | None = None
    # Note: validation is handled internally via feedback loop - not exposed to users


@dataclass
class CustomDocJob:
    """State of an in-progress custom document generation job."""

    job_id: str
    product_id: str
    user_id: str
    status: str  # "generating" | "completed" | "failed"
    progress: str | None = None  # Current stage message
    content: str | None = None  # Generated content when complete
    suggested_title: str | None = None
    error: str | None = None
