"""
GitHub API read operations.

Provides all read-only operations for fetching repository data:
- Repository metadata and details
- File trees and contents
- Language statistics
- Contributors
- Commit statistics
"""

import asyncio
import base64
import logging
import re
from typing import Any

import httpx

from app.services.github.cache import (
    cached_github_call,
    contributors_cache,
    languages_cache,
    repo_details_cache,
    tree_cache,
)
from app.services.github.constants import (
    GITHUB_LANGUAGE_COLORS,
    KEY_FILES,
)
from app.services.github.exceptions import GitHubAPIError
from app.services.github.helpers import RateLimitInfo, handle_error_response
from app.services.github.http_client import get_github_client
from app.services.github.types import (
    CommitStats,
    ContributorInfo,
    GitHubRepo,
    GitHubReposResponse,
    LanguageStat,
    RepoContext,
    RepoFile,
    RepoTree,
    RepoTreeItem,
)

logger = logging.getLogger(__name__)


class GitHubReadOperations:
    """
    Read-only operations for GitHub API.

    This class provides all methods for fetching data from GitHub repositories
    without modifying them.

    Uses a shared HTTP client singleton for connection pooling. This eliminates
    ~50-100ms SSL handshake overhead per request by reusing connections.
    """

    BASE_URL = "https://api.github.com"
    API_VERSION = "2022-11-28"

    def __init__(self, token: str):
        self.token = token
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": self.API_VERSION,
        }

    def _normalize_repo(self, data: dict[str, Any]) -> GitHubRepo:
        """Convert GitHub API response to GitHubRepo dataclass."""
        license_data = data.get("license")
        license_name = license_data.get("spdx_id") if license_data else None

        return GitHubRepo(
            github_id=data["id"],
            name=data["name"],
            full_name=data["full_name"],
            description=data.get("description"),
            url=data["html_url"],
            default_branch=data.get("default_branch", "main"),
            is_private=data.get("private", False),
            language=data.get("language"),
            stars_count=data.get("stargazers_count", 0),
            forks_count=data.get("forks_count", 0),
            updated_at=data.get("updated_at", ""),
            created_at=data.get("created_at"),
            pushed_at=data.get("pushed_at"),
            open_issues_count=data.get("open_issues_count", 0),
            license_name=license_name,
        )

    async def get_user_repos(
        self,
        page: int = 1,
        per_page: int = 30,
        visibility: str = "all",
        affiliation: str = "owner,collaborator,organization_member",
        sort: str = "updated",
        direction: str = "desc",
    ) -> GitHubReposResponse:
        """
        Fetch repositories for the authenticated user.

        Args:
            page: Page number (1-indexed)
            per_page: Items per page (max 100)
            visibility: Filter by visibility ('all', 'public', 'private')
            affiliation: Filter by affiliation
            sort: Sort by ('created', 'updated', 'pushed', 'full_name')
            direction: Sort direction ('asc', 'desc')

        Returns:
            GitHubReposResponse with repos and pagination info
        """
        params: dict[str, str | int] = {
            "page": page,
            "per_page": min(per_page, 100),
            "visibility": visibility,
            "affiliation": affiliation,
            "sort": sort,
            "direction": direction,
        }

        client = get_github_client()
        response = await client.get(
            f"{self.BASE_URL}/user/repos",
            headers=self._headers,
            params=params,
        )

        rate_info = RateLimitInfo(response)

        if response.status_code == 401:
            raise GitHubAPIError("Invalid or expired GitHub token", 401)
        elif response.status_code == 403:
            if rate_info.is_exhausted:
                raise GitHubAPIError(
                    "GitHub API rate limit exceeded",
                    403,
                    rate_limit_reset=rate_info.reset_timestamp,
                )
            raise GitHubAPIError("GitHub API forbidden", 403)
        elif response.status_code != 200:
            raise GitHubAPIError(f"GitHub API error: {response.status_code}", response.status_code)

        data = response.json()
        repos = [self._normalize_repo(r) for r in data]

        link_header = response.headers.get("Link", "")
        has_more = 'rel="next"' in link_header

        return GitHubReposResponse(
            repos=repos,
            total_count=len(repos),
            has_more=has_more,
            rate_limit_remaining=int(rate_info.remaining) if rate_info.remaining else None,
        )

    @cached_github_call(repo_details_cache)
    async def get_repo_details(self, owner: str, repo: str) -> GitHubRepo:
        """
        Fetch detailed information for a specific repository.

        Results are cached for 10 minutes to reduce API calls for metadata
        that changes infrequently (stars, forks, description).

        Args:
            owner: Repository owner (username or org)
            repo: Repository name

        Returns:
            GitHubRepo with full repository details
        """
        client = get_github_client()
        response = await client.get(
            f"{self.BASE_URL}/repos/{owner}/{repo}",
            headers=self._headers,
        )

        rate_info = RateLimitInfo(response)

        if response.status_code == 401:
            raise GitHubAPIError("Invalid or expired GitHub token", 401)
        elif response.status_code == 404:
            raise GitHubAPIError(f"Repository {owner}/{repo} not found", 404)
        elif response.status_code == 403:
            if rate_info.is_exhausted:
                raise GitHubAPIError(
                    "GitHub API rate limit exceeded",
                    403,
                    rate_limit_reset=rate_info.reset_timestamp,
                )
            raise GitHubAPIError("GitHub API forbidden", 403)
        elif response.status_code != 200:
            raise GitHubAPIError(f"GitHub API error: {response.status_code}", response.status_code)

        return self._normalize_repo(response.json())

    async def get_repo_by_id(self, repo_id: int) -> GitHubRepo:
        """
        Fetch repository details by GitHub repository ID.

        This is useful when GitHub redirects to a repository ID-based URL
        after a rename/transfer, and we need to resolve the current owner/repo.

        Args:
            repo_id: GitHub repository ID (immutable, doesn't change on rename)

        Returns:
            GitHubRepo with full repository details including current owner/name
        """
        client = get_github_client()
        response = await client.get(
            f"{self.BASE_URL}/repositories/{repo_id}",
            headers=self._headers,
        )

        rate_info = RateLimitInfo(response)

        if response.status_code == 401:
            raise GitHubAPIError("Invalid or expired GitHub token", 401)
        elif response.status_code == 404:
            raise GitHubAPIError(f"Repository with ID {repo_id} not found", 404)
        elif response.status_code == 403:
            if rate_info.is_exhausted:
                raise GitHubAPIError(
                    "GitHub API rate limit exceeded",
                    403,
                    rate_limit_reset=rate_info.reset_timestamp,
                )
            raise GitHubAPIError("GitHub API forbidden", 403)
        elif response.status_code != 200:
            raise GitHubAPIError(f"GitHub API error: {response.status_code}", response.status_code)

        return self._normalize_repo(response.json())

    async def get_authenticated_user(self) -> dict[str, Any]:
        """
        Fetch authenticated user info.

        Returns:
            Dict with user info (login, name, avatar_url, etc.)
        """
        client = get_github_client()
        response = await client.get(
            f"{self.BASE_URL}/user",
            headers=self._headers,
            timeout=10.0,
        )

        if response.status_code == 401:
            raise GitHubAPIError("Invalid or expired GitHub token", 401)
        elif response.status_code != 200:
            raise GitHubAPIError(f"GitHub API error: {response.status_code}", response.status_code)

        result: dict[str, Any] = response.json()
        return result

    @cached_github_call(tree_cache)
    async def get_repo_tree(
        self,
        owner: str,
        repo: str,
        branch: str = "main",
    ) -> RepoTree:
        """
        Fetch the complete file tree for a repository.

        Uses the Git Trees API with recursive=1 to get all files in a single call.
        Results are cached for 5 minutes - tree changes with commits but within
        a session we typically see the same data.

        Args:
            owner: Repository owner (username or org)
            repo: Repository name
            branch: Branch name (default: "main")

        Returns:
            RepoTree with file paths, directory paths, and truncation status
        """
        client = get_github_client()
        response = await client.get(
            f"{self.BASE_URL}/repos/{owner}/{repo}/git/trees/{branch}",
            headers=self._headers,
            params={"recursive": "1"},
        )

        handle_error_response(response, f"{owner}/{repo}")

        data = response.json()

        files: list[str] = []
        directories: list[str] = []
        all_items: list[RepoTreeItem] = []

        for item in data.get("tree", []):
            tree_item = RepoTreeItem(
                path=item["path"],
                type=item["type"],
                size=item.get("size"),
                sha=item["sha"],
            )
            all_items.append(tree_item)

            if item["type"] == "blob":
                files.append(item["path"])
            elif item["type"] == "tree":
                directories.append(item["path"])

        return RepoTree(
            sha=data["sha"],
            files=files,
            directories=directories,
            all_items=all_items,
            truncated=data.get("truncated", False),
        )

    async def get_file_content(
        self,
        owner: str,
        repo: str,
        path: str,
        branch: str = "main",
        max_size: int = 100_000,
    ) -> RepoFile | None:
        """
        Fetch the content of a specific file from a repository.

        Args:
            owner: Repository owner
            repo: Repository name
            path: File path within the repository
            branch: Branch name (default: "main")
            max_size: Maximum file size in bytes to fetch (default: 100KB)

        Returns:
            RepoFile with decoded content, or None if file is too large/binary
        """
        client = get_github_client()
        response = await client.get(
            f"{self.BASE_URL}/repos/{owner}/{repo}/contents/{path}",
            headers=self._headers,
            params={"ref": branch},
        )

        if response.status_code == 404:
            return None

        handle_error_response(response, f"{owner}/{repo}")

        data = response.json()

        if data.get("type") != "file":
            return None

        size = data.get("size", 0)
        if size > max_size:
            return None

        content_b64 = data.get("content")
        if not content_b64:
            return None

        try:
            content_bytes = base64.b64decode(content_b64)
            content = content_bytes.decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return None

        return RepoFile(
            path=path,
            content=content,
            size=size,
            sha=data["sha"],
            encoding=data.get("encoding", "base64"),
        )

    @cached_github_call(languages_cache)
    async def get_repo_languages(
        self,
        owner: str,
        repo: str,
    ) -> list[LanguageStat]:
        """
        Fetch language breakdown for a repository.

        Results are cached for 1 hour - language breakdown rarely changes
        and is expensive to compute on GitHub's side.

        Args:
            owner: Repository owner
            repo: Repository name

        Returns:
            List of LanguageStat sorted by percentage (descending)
        """
        client = get_github_client()
        response = await client.get(
            f"{self.BASE_URL}/repos/{owner}/{repo}/languages",
            headers=self._headers,
            timeout=15.0,
        )

        handle_error_response(response, f"{owner}/{repo}")

        data: dict[str, int] = response.json()

        if not data:
            return []

        total_bytes = sum(data.values())
        if total_bytes == 0:
            return []

        languages = [
            LanguageStat(
                name=name,
                bytes=byte_count,
                percentage=round((byte_count / total_bytes) * 100, 1),
                color=GITHUB_LANGUAGE_COLORS.get(name, "#8b8b8b"),
            )
            for name, byte_count in data.items()
        ]

        languages.sort(key=lambda x: x.percentage, reverse=True)
        return languages

    @cached_github_call(contributors_cache)
    async def get_repo_contributors(
        self,
        owner: str,
        repo: str,
        limit: int = 10,
    ) -> list[ContributorInfo]:
        """
        Fetch top contributors for a repository.

        Results are cached for 1 hour - contributor list rarely changes
        and is frequently accessed for product cards.

        Args:
            owner: Repository owner
            repo: Repository name
            limit: Maximum number of contributors to return (default: 10)

        Returns:
            List of ContributorInfo sorted by contributions (descending)
        """
        client = get_github_client()
        response = await client.get(
            f"{self.BASE_URL}/repos/{owner}/{repo}/contributors",
            headers=self._headers,
            params={"per_page": limit, "anon": "false"},
            timeout=15.0,
        )

        if response.status_code == 204:
            return []

        handle_error_response(response, f"{owner}/{repo}")

        data: list[dict[str, Any]] = response.json()

        return [
            ContributorInfo(
                login=contrib["login"],
                avatar_url=contrib.get("avatar_url"),
                contributions=contrib.get("contributions", 0),
            )
            for contrib in data[:limit]
        ]

    async def get_commit_stats(
        self,
        owner: str,
        repo: str,
        branch: str = "main",
    ) -> CommitStats:
        """
        Fetch commit statistics for a repository.

        Args:
            owner: Repository owner
            repo: Repository name
            branch: Branch name (default: "main")

        Returns:
            CommitStats with total commits and date range
        """
        client = get_github_client()
        response = await client.get(
            f"{self.BASE_URL}/repos/{owner}/{repo}/commits",
            headers=self._headers,
            params={"sha": branch, "per_page": 1},
            timeout=15.0,
        )

        if response.status_code != 200:
            return CommitStats(total_commits=0, first_commit_date=None, last_commit_date=None)

        commits = response.json()
        if not commits:
            return CommitStats(total_commits=0, first_commit_date=None, last_commit_date=None)

        last_commit_date = commits[0].get("commit", {}).get("committer", {}).get("date")

        link_header = response.headers.get("Link", "")
        total_commits = 1

        if 'rel="last"' in link_header:
            match = re.search(r'page=(\d+)>; rel="last"', link_header)
            if match:
                total_commits = int(match.group(1))

        first_commit_date = None
        if total_commits > 1:
            last_page_response = await client.get(
                f"{self.BASE_URL}/repos/{owner}/{repo}/commits",
                headers=self._headers,
                params={"sha": branch, "per_page": 1, "page": total_commits},
                timeout=15.0,
            )

            if last_page_response.status_code == 200:
                last_page_commits = last_page_response.json()
                if last_page_commits:
                    first_commit_date = (
                        last_page_commits[0].get("commit", {}).get("committer", {}).get("date")
                    )
        else:
            first_commit_date = last_commit_date

        return CommitStats(
            total_commits=total_commits,
            first_commit_date=first_commit_date,
            last_commit_date=last_commit_date,
        )

    async def get_commit_detail(
        self,
        owner: str,
        repo: str,
        sha: str,
        timeout: float = 5.0,
    ) -> dict[str, int] | None:
        """
        Fetch detailed stats for a single commit.

        Args:
            owner: Repository owner
            repo: Repository name
            sha: Commit SHA
            timeout: Request timeout in seconds (default: 5s for performance)

        Returns:
            Dict with additions, deletions, files_changed or None if fetch fails
        """
        try:
            client = get_github_client()
            response = await client.get(
                f"{self.BASE_URL}/repos/{owner}/{repo}/commits/{sha}",
                headers=self._headers,
                timeout=timeout,
            )

            if response.status_code != 200:
                return None

            data = response.json()
            stats = data.get("stats", {})
            files = data.get("files", [])

            return {
                "additions": stats.get("additions", 0),
                "deletions": stats.get("deletions", 0),
                "files_changed": len(files),
            }
        except (httpx.TimeoutException, httpx.RequestError):
            return None

    async def get_commits_for_timeline(
        self,
        owner: str,
        repo: str,
        branch: str | None = None,
        per_page: int = 50,
        sha_cursor: str | None = None,
        path: str | None = None,
    ) -> tuple[list[dict[str, Any]], bool]:
        """
        Fetch commits for timeline display.

        Args:
            owner: Repository owner
            repo: Repository name
            branch: Branch name (default: repo's default branch)
            per_page: Number of commits per page
            sha_cursor: SHA to start from (for pagination)
            path: Optional file path to filter commits (partial match)

        Returns:
            Tuple of (commits list, has_more flag)
        """
        params: dict[str, str | int] = {"per_page": per_page + 1}  # Fetch one extra for has_more
        if sha_cursor:
            # Pagination: continue from this commit SHA.
            # GitHub returns ancestors of the given SHA, which naturally
            # follows the branch's commit history — no separate branch param needed.
            params["sha"] = sha_cursor
        elif branch:
            params["sha"] = branch
        if path:
            params["path"] = path

        client = get_github_client()
        response = await client.get(
            f"{self.BASE_URL}/repos/{owner}/{repo}/commits",
            headers=self._headers,
            params=params,
        )

        handle_error_response(response, f"{owner}/{repo}")

        commits: list[dict[str, Any]] = response.json()
        has_more = len(commits) > per_page
        return commits[:per_page], has_more

    async def get_commit_files(
        self,
        owner: str,
        repo: str,
        sha: str,
        timeout: float = 10.0,
    ) -> list[dict[str, Any]] | None:
        """
        Fetch detailed file changes for a single commit.

        Args:
            owner: Repository owner
            repo: Repository name
            sha: Commit SHA
            timeout: Request timeout in seconds

        Returns:
            List of file change dicts with filename, status, additions, deletions,
            or None if fetch fails
        """
        try:
            client = get_github_client()
            response = await client.get(
                f"{self.BASE_URL}/repos/{owner}/{repo}/commits/{sha}",
                headers=self._headers,
                timeout=timeout,
            )

            if response.status_code != 200:
                return None

            data = response.json()
            files = data.get("files", [])

            return [
                {
                    "filename": f.get("filename", ""),
                    "status": f.get("status", "modified"),
                    "additions": f.get("additions", 0),
                    "deletions": f.get("deletions", 0),
                }
                for f in files
            ]
        except (httpx.TimeoutException, httpx.RequestError):
            return None

    async def get_recent_commits(
        self,
        owner: str,
        repo: str,
        per_page: int = 10,
    ) -> list[dict[str, Any]]:
        """Fetch recent commits for a repo (lightweight, for agent context).

        Returns list of dicts with sha, message, author, date.
        """
        client = get_github_client()
        response = await client.get(
            f"{self.BASE_URL}/repos/{owner}/{repo}/commits",
            headers=self._headers,
            params={"per_page": per_page},
            timeout=10.0,
        )
        if response.status_code != 200:
            return []
        commits = response.json()
        return [
            {
                "sha": c["sha"][:7],
                "message": c.get("commit", {}).get("message", "").split("\n")[0],
                "author": c.get("commit", {}).get("author", {}).get("name", ""),
                "date": c.get("commit", {}).get("author", {}).get("date", ""),
            }
            for c in commits
        ]

    async def get_merged_pulls_count(
        self,
        owner: str,
        repo: str,
        since: str,
        per_page: int = 100,
    ) -> int:
        """Count merged pull requests since a given date.

        Fetches closed PRs sorted by updated date and counts those with
        merged_at >= since. Stops early when PRs are older than the cutoff.

        Args:
            owner: Repository owner
            repo: Repository name
            since: ISO date string cutoff (e.g. "2026-03-01T00:00:00Z")
            per_page: Page size for the API call

        Returns:
            Number of merged PRs since the cutoff date
        """
        client = get_github_client()
        merged_count = 0
        page = 1

        while True:
            response = await client.get(
                f"{self.BASE_URL}/repos/{owner}/{repo}/pulls",
                headers=self._headers,
                params={
                    "state": "closed",
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": per_page,
                    "page": page,
                },
                timeout=10.0,
            )
            if response.status_code != 200:
                break

            pulls = response.json()
            if not pulls:
                break

            for pr in pulls:
                updated_at = pr.get("updated_at", "")
                if updated_at < since:
                    return merged_count
                merged_at = pr.get("merged_at")
                if merged_at and merged_at >= since:
                    merged_count += 1

            if len(pulls) < per_page:
                break
            page += 1
            if page > 3:
                break

        return merged_count

    async def get_open_pulls(
        self,
        owner: str,
        repo: str,
        per_page: int = 10,
    ) -> list[dict[str, Any]]:
        """Fetch open pull requests for a repo (lightweight, for agent context)."""
        client = get_github_client()
        response = await client.get(
            f"{self.BASE_URL}/repos/{owner}/{repo}/pulls",
            headers=self._headers,
            params={"state": "open", "per_page": per_page, "sort": "updated"},
            timeout=10.0,
        )
        if response.status_code != 200:
            return []
        pulls = response.json()
        return [
            {
                "number": pr["number"],
                "title": pr.get("title", ""),
                "author": pr.get("user", {}).get("login", ""),
                "updated": pr.get("updated_at", ""),
            }
            for pr in pulls
        ]

    async def get_open_issues(
        self,
        owner: str,
        repo: str,
        per_page: int = 10,
    ) -> list[dict[str, Any]]:
        """Fetch open issues (excluding PRs) for a repo (lightweight, for agent context)."""
        client = get_github_client()
        response = await client.get(
            f"{self.BASE_URL}/repos/{owner}/{repo}/issues",
            headers=self._headers,
            params={"state": "open", "per_page": per_page, "sort": "updated"},
            timeout=10.0,
        )
        if response.status_code != 200:
            return []
        issues = response.json()
        # GitHub issues API includes PRs; filter them out
        return [
            {
                "number": issue["number"],
                "title": issue.get("title", ""),
                "author": issue.get("user", {}).get("login", ""),
                "labels": [lbl.get("name", "") for lbl in issue.get("labels", [])],
            }
            for issue in issues
            if "pull_request" not in issue
        ]

    async def get_key_files(
        self,
        owner: str,
        repo: str,
        branch: str = "main",
        tree: RepoTree | None = None,
        max_concurrent: int = 5,
    ) -> dict[str, str]:
        """
        Fetch contents of key files for AI analysis (parallel).

        Args:
            owner: Repository owner
            repo: Repository name
            branch: Branch name (default: "main")
            tree: Optional pre-fetched RepoTree (avoids extra API call)
            max_concurrent: Maximum concurrent requests (default: 5)

        Returns:
            Dict mapping file paths to their contents
        """
        if tree:
            existing_files = set(tree.files)
            files_to_fetch = [f for f in KEY_FILES if f in existing_files]
        else:
            files_to_fetch = list(KEY_FILES)

        if not files_to_fetch:
            return {}

        semaphore = asyncio.Semaphore(max_concurrent)

        async def fetch_with_limit(file_path: str) -> tuple[str, str | None]:
            async with semaphore:
                content = await self.get_file_content(owner, repo, file_path, branch)
                return (file_path, content.content if content else None)

        tasks = [fetch_with_limit(fp) for fp in files_to_fetch]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        return {
            path: content
            for result in results
            if isinstance(result, tuple) and (path := result[0]) and (content := result[1])
        }

    async def fetch_files_by_paths(
        self,
        owner: str,
        repo: str,
        paths: list[str],
        branch: str = "main",
        max_concurrent: int = 5,
        max_size: int = 100_000,
    ) -> dict[str, str]:
        """
        Fetch contents of specific files by their paths.

        Args:
            owner: Repository owner
            repo: Repository name
            paths: List of file paths to fetch
            branch: Branch name (default: "main")
            max_concurrent: Maximum concurrent requests (default: 5)
            max_size: Maximum file size in bytes (default: 100KB)

        Returns:
            Dict mapping file paths to their contents (excludes missing/binary files)
        """
        if not paths:
            return {}

        semaphore = asyncio.Semaphore(max_concurrent)

        async def fetch_with_limit(file_path: str) -> tuple[str, str | None]:
            async with semaphore:
                content = await self.get_file_content(
                    owner, repo, file_path, branch, max_size=max_size
                )
                return (file_path, content.content if content else None)

        tasks = [fetch_with_limit(fp) for fp in paths]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        return {
            path: content
            for result in results
            if isinstance(result, tuple) and (path := result[0]) and (content := result[1])
        }

    async def get_repo_context(
        self,
        owner: str,
        repo: str,
        branch: str | None = None,
        description: str | None = None,
    ) -> RepoContext:
        """
        Fetch complete context for a repository for AI analysis.

        This is the main entry point for gathering all information needed
        to analyze a repository.

        Args:
            owner: Repository owner
            repo: Repository name
            branch: Branch name (if None, fetches repo details to get default branch)
            description: Repository description (if known, avoids extra API call)

        Returns:
            RepoContext with all gathered information
        """
        errors: list[str] = []
        tree: RepoTree | None = None
        files: dict[str, str] = {}
        languages: list[LanguageStat] = []
        contributors: list[ContributorInfo] = []
        commit_stats: CommitStats | None = None

        stars_count = 0
        forks_count = 0
        open_issues_count = 0
        created_at: str | None = None
        updated_at: str | None = None
        pushed_at: str | None = None
        license_name: str | None = None

        repo_details: GitHubRepo | None = None
        try:
            repo_details = await self.get_repo_details(owner, repo)
            if branch is None:
                branch = repo_details.default_branch
            if description is None:
                description = repo_details.description

            stars_count = repo_details.stars_count
            forks_count = repo_details.forks_count
            open_issues_count = repo_details.open_issues_count
            created_at = repo_details.created_at
            updated_at = repo_details.updated_at
            pushed_at = repo_details.pushed_at
            license_name = repo_details.license_name
        except GitHubAPIError as e:
            errors.append(f"Failed to get repo details: {e.message}")
            if branch is None:
                branch = "main"

        try:
            tree = await self.get_repo_tree(owner, repo, branch)
            if tree.truncated:
                errors.append("Repository tree was truncated (very large repo)")
        except GitHubAPIError as e:
            errors.append(f"Failed to get repo tree: {e.message}")

        try:
            files = await self.get_key_files(owner, repo, branch, tree)
        except GitHubAPIError as e:
            errors.append(f"Failed to get key files: {e.message}")

        try:
            languages = await self.get_repo_languages(owner, repo)
        except GitHubAPIError as e:
            errors.append(f"Failed to get languages: {e.message}")

        try:
            contributors = await self.get_repo_contributors(owner, repo)
        except GitHubAPIError as e:
            errors.append(f"Failed to get contributors: {e.message}")

        try:
            commit_stats = await self.get_commit_stats(owner, repo, branch)
        except GitHubAPIError as e:
            errors.append(f"Failed to get commit stats: {e.message}")

        return RepoContext(
            owner=owner,
            repo=repo,
            full_name=f"{owner}/{repo}",
            default_branch=branch,
            description=description,
            tree=tree,
            files=files,
            languages=languages,
            contributors=contributors,
            errors=errors,
            stars_count=stars_count,
            forks_count=forks_count,
            open_issues_count=open_issues_count,
            created_at=created_at,
            updated_at=updated_at,
            pushed_at=pushed_at,
            license_name=license_name,
            commit_stats=commit_stats,
        )
