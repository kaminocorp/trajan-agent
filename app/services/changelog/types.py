"""Types for the changelog generation service."""

from dataclasses import dataclass, field


@dataclass
class ChangelogGroupedEntry:
    """A single changelog entry as returned by the AI grouping call."""

    title: str
    category: str  # added, changed, fixed, removed, security, infrastructure, other
    summary: str
    commit_shas: list[str]


@dataclass
class BatchResult:
    """Result of processing one batch of commits."""

    entries_created: int
    commits_processed: int


@dataclass
class GenerationProgress:
    """Progress state for the frontend to poll."""

    stage: str  # "fetching" | "processing" | "complete" | "error"
    message: str
    batch_current: int = 0
    batch_total: int = 0
    entries_created: int = 0
    commits_processed: int = 0


@dataclass
class GenerationResult:
    """Final result of a full changelog generation run."""

    entries_created: int = 0
    commits_processed: int = 0
    batches_completed: int = 0
    batches_total: int = 0
    skipped_reason: str | None = None
    errors: list[str] = field(default_factory=list)
