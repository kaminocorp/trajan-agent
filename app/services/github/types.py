"""Data types for GitHub API responses."""

from dataclasses import dataclass, field


@dataclass
class GitHubRepo:
    """Normalized GitHub repository data."""

    github_id: int
    name: str
    full_name: str
    description: str | None
    url: str
    default_branch: str
    is_private: bool
    language: str | None
    stars_count: int
    forks_count: int
    updated_at: str
    # Extended metadata fields (Phase 1 refactoring)
    created_at: str | None = None
    pushed_at: str | None = None
    open_issues_count: int = 0
    license_name: str | None = None  # SPDX identifier (e.g., "MIT", "Apache-2.0")


@dataclass
class GitHubReposResponse:
    """Response from listing GitHub repos."""

    repos: list[GitHubRepo]
    total_count: int
    has_more: bool
    rate_limit_remaining: int | None


@dataclass
class RepoTreeItem:
    """Single item in a repository tree."""

    path: str
    type: str  # "blob" (file) or "tree" (directory)
    size: int | None  # Size in bytes (only for blobs)
    sha: str


@dataclass
class RepoTree:
    """Repository file tree structure."""

    sha: str
    files: list[str]  # List of file paths (blobs only)
    directories: list[str]  # List of directory paths
    all_items: list[RepoTreeItem]  # Full tree data
    truncated: bool  # True if tree was too large and truncated


@dataclass
class RepoFile:
    """Contents of a single file from a repository."""

    path: str
    content: str  # Decoded text content
    size: int
    sha: str
    encoding: str  # Original encoding (usually "base64")


@dataclass
class LanguageStat:
    """Language statistics for a repository."""

    name: str
    bytes: int
    percentage: float
    color: str  # Hex color for display


@dataclass
class ContributorInfo:
    """Contributor information."""

    login: str
    avatar_url: str | None
    contributions: int  # Number of commits


@dataclass
class CommitStats:
    """Commit statistics for a repository."""

    total_commits: int
    first_commit_date: str | None  # ISO 8601 date string
    last_commit_date: str | None  # ISO 8601 date string


@dataclass
class PullRequestInfo:
    """Information about a created or existing pull request."""

    number: int
    url: str  # API URL
    html_url: str  # Browser URL
    head: str  # Source branch
    base: str  # Target branch
    state: str  # "open", "closed", "merged"


@dataclass
class RepoContext:
    """Aggregated context for a repository, used for AI analysis."""

    owner: str
    repo: str
    full_name: str
    default_branch: str
    description: str | None
    tree: RepoTree | None
    files: dict[str, str]  # path -> content mapping
    languages: list[LanguageStat]
    contributors: list[ContributorInfo]
    errors: list[str] = field(default_factory=list)  # Any errors during fetching

    # Extended metadata fields (Phase 1 refactoring)
    stars_count: int = 0
    forks_count: int = 0
    open_issues_count: int = 0
    created_at: str | None = None
    updated_at: str | None = None
    pushed_at: str | None = None
    license_name: str | None = None

    # Commit stats (from separate API call)
    commit_stats: CommitStats | None = None
