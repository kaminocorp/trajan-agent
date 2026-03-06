from dataclasses import dataclass
from typing import Literal


@dataclass
class TimelineEvent:
    """Unified timeline event."""

    id: str  # "commit:{sha}"
    event_type: Literal["commit"]
    timestamp: str  # ISO 8601
    repository_id: str  # Internal repo UUID
    repository_name: str
    repository_full_name: str  # owner/repo

    # Commit fields
    commit_sha: str
    commit_message: str
    commit_author: str
    commit_author_avatar: str | None
    commit_url: str

    # Optional fields
    commit_author_login: str | None = None  # GitHub username (from API "author.login")
    additions: int | None = None
    deletions: int | None = None
    files_changed: int | None = None


@dataclass
class TimelineResponse:
    """Paginated timeline response."""

    events: list[TimelineEvent]
    has_more: bool
    next_cursor: str | None  # timestamp:sha for pagination
