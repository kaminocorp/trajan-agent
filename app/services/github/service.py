"""
GitHub API service for repository operations.

This is the main entry point for all GitHub API interactions.
It composes read and write operations from separate modules.

Handles:
- Fetching user repositories
- Getting repository details
- Fetching repository contents (trees, files)
- Language and contributor statistics
- Creating commits and updating files
"""

from typing import Any

from app.services.github.read_operations import GitHubReadOperations
from app.services.github.types import (
    CommitStats,
    ContributorInfo,
    GitHubRepo,
    GitHubReposResponse,
    LanguageStat,
    PullRequestInfo,
    RepoContext,
    RepoFile,
    RepoTree,
)
from app.services.github.write_operations import GitHubWriteOperations


def calculate_lines_of_code(files: dict[str, str]) -> int:
    """
    Count total lines of code across all fetched files.

    This provides an estimate based on key files only (README, config files,
    main source files). For accurate total LOC, you would need to fetch all
    files in the repository which is expensive.

    Args:
        files: Dict mapping file paths to their content

    Returns:
        Total line count across all files
    """
    if not files:
        return 0
    return sum(content.count("\n") + 1 for content in files.values() if content)


class GitHubService(GitHubReadOperations, GitHubWriteOperations):
    """
    Service for interacting with GitHub REST API.

    Composes read and write operations through multiple inheritance.
    This class is the main entry point and maintains backwards compatibility
    with existing code that uses GitHubService directly.

    Read operations (from GitHubReadOperations):
        - get_user_repos
        - get_repo_details
        - get_authenticated_user
        - get_repo_tree
        - get_file_content
        - get_repo_languages
        - get_repo_contributors
        - get_commit_stats
        - get_key_files
        - fetch_files_by_paths
        - get_repo_context

    Write operations (from GitHubWriteOperations):
        - create_commit
        - get_file_sha
        - branch_exists
        - create_branch
        - create_pull_request
    """

    BASE_URL = "https://api.github.com"
    API_VERSION = "2022-11-28"

    def __init__(self, token: str):
        """
        Initialize the GitHub service.

        Args:
            token: GitHub personal access token or OAuth token
        """
        # Initialize both parent classes
        GitHubReadOperations.__init__(self, token)
        GitHubWriteOperations.__init__(self, token)

    # Re-export type hints for IDE support
    async def get_installation_repos(
        self,
        page: int = 1,
        per_page: int = 30,
    ) -> GitHubReposResponse:
        """Fetch repositories accessible to a GitHub App installation."""
        return await GitHubReadOperations.get_installation_repos(self, page, per_page)

    async def get_user_repos(
        self,
        page: int = 1,
        per_page: int = 30,
        visibility: str = "all",
        affiliation: str = "owner,collaborator,organization_member",
        sort: str = "updated",
        direction: str = "desc",
    ) -> GitHubReposResponse:
        """Fetch repositories for the authenticated user."""
        return await GitHubReadOperations.get_user_repos(
            self, page, per_page, visibility, affiliation, sort, direction
        )

    async def get_repo_details(self, owner: str, repo: str) -> GitHubRepo:
        """Fetch detailed information for a specific repository."""
        return await GitHubReadOperations.get_repo_details(self, owner, repo)

    async def get_repo_by_id(self, repo_id: int) -> GitHubRepo:
        """Fetch repository details by GitHub repository ID."""
        return await GitHubReadOperations.get_repo_by_id(self, repo_id)

    async def get_authenticated_user(self) -> dict[str, Any]:
        """Fetch authenticated user info."""
        return await GitHubReadOperations.get_authenticated_user(self)

    async def get_repo_tree(
        self,
        owner: str,
        repo: str,
        branch: str = "main",
    ) -> RepoTree:
        """Fetch the complete file tree for a repository."""
        return await GitHubReadOperations.get_repo_tree(self, owner, repo, branch)

    async def get_file_content(
        self,
        owner: str,
        repo: str,
        path: str,
        branch: str = "main",
        max_size: int = 100_000,
    ) -> RepoFile | None:
        """Fetch the content of a specific file from a repository."""
        return await GitHubReadOperations.get_file_content(
            self, owner, repo, path, branch, max_size
        )

    async def get_repo_languages(
        self,
        owner: str,
        repo: str,
    ) -> list[LanguageStat]:
        """Fetch language breakdown for a repository."""
        return await GitHubReadOperations.get_repo_languages(self, owner, repo)

    async def get_repo_contributors(
        self,
        owner: str,
        repo: str,
        limit: int = 10,
    ) -> list[ContributorInfo]:
        """Fetch top contributors for a repository."""
        return await GitHubReadOperations.get_repo_contributors(self, owner, repo, limit)

    async def get_commit_stats(
        self,
        owner: str,
        repo: str,
        branch: str = "main",
    ) -> CommitStats:
        """Fetch commit statistics for a repository."""
        return await GitHubReadOperations.get_commit_stats(self, owner, repo, branch)

    async def get_key_files(
        self,
        owner: str,
        repo: str,
        branch: str = "main",
        tree: RepoTree | None = None,
        max_concurrent: int = 5,
    ) -> dict[str, str]:
        """Fetch contents of key files for AI analysis (parallel)."""
        return await GitHubReadOperations.get_key_files(
            self, owner, repo, branch, tree, max_concurrent
        )

    async def fetch_files_by_paths(
        self,
        owner: str,
        repo: str,
        paths: list[str],
        branch: str = "main",
        max_concurrent: int = 5,
        max_size: int = 100_000,
    ) -> dict[str, str]:
        """Fetch contents of specific files by their paths."""
        return await GitHubReadOperations.fetch_files_by_paths(
            self, owner, repo, paths, branch, max_concurrent, max_size
        )

    async def get_repo_context(
        self,
        owner: str,
        repo: str,
        branch: str | None = None,
        description: str | None = None,
    ) -> RepoContext:
        """Fetch complete context for a repository for AI analysis."""
        return await GitHubReadOperations.get_repo_context(self, owner, repo, branch, description)

    async def create_commit(
        self,
        owner: str,
        repo: str,
        files: list[dict[str, str]],
        message: str,
        branch: str = "main",
    ) -> str:
        """Create a commit with multiple file changes."""
        return await GitHubWriteOperations.create_commit(self, owner, repo, files, message, branch)

    async def get_file_sha(
        self,
        owner: str,
        repo: str,
        path: str,
        branch: str = "main",
    ) -> str | None:
        """Get the SHA of a specific file in the repository."""
        return await GitHubWriteOperations.get_file_sha(self, owner, repo, path, branch)

    async def branch_exists(self, owner: str, repo: str, branch: str) -> bool:
        """Check whether a branch exists in the repository."""
        return await GitHubWriteOperations.branch_exists(self, owner, repo, branch)

    async def create_branch(
        self,
        owner: str,
        repo: str,
        branch: str,
        from_branch: str = "main",
    ) -> str:
        """Create a new branch from an existing branch. Idempotent."""
        return await GitHubWriteOperations.create_branch(
            self, owner, repo, branch, from_branch
        )

    async def create_pull_request(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> PullRequestInfo:
        """Create a pull request, or return existing if one already exists."""
        return await GitHubWriteOperations.create_pull_request(
            self, owner, repo, title, body, head, base
        )
