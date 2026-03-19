"""
GitHub API write operations.

Provides all write operations for modifying repository content:
- Creating commits with multiple file changes
- Getting file SHAs for updates
- Branch creation and management
- Pull request creation
"""

import logging

from app.services.github.exceptions import GitHubAPIError
from app.services.github.helpers import handle_error_response
from app.services.github.http_client import get_github_client
from app.services.github.types import PullRequestInfo

logger = logging.getLogger(__name__)


class GitHubWriteOperations:
    """
    Write operations for GitHub API.

    This class provides all methods for modifying GitHub repository content.
    Uses the Git Data API for atomic commits.
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

    async def create_commit(
        self,
        owner: str,
        repo: str,
        files: list[dict[str, str]],
        message: str,
        branch: str = "main",
    ) -> str:
        """
        Create a commit with multiple file changes.

        Uses the Git Data API for atomic commits:
        1. Get the latest commit SHA for the branch
        2. Create blobs for each file
        3. Create a new tree with file changes
        4. Create the commit object
        5. Update the branch reference

        Args:
            owner: Repository owner
            repo: Repository name
            files: List of dicts with "path" and "content" keys
            message: Commit message
            branch: Branch name (default: "main")

        Returns:
            The new commit SHA
        """
        client = get_github_client()

        # 1. Get the latest commit SHA for the branch
        ref_response = await client.get(
            f"{self.BASE_URL}/repos/{owner}/{repo}/git/refs/heads/{branch}",
            headers=self._headers,
        )

        if ref_response.status_code == 404:
            raise GitHubAPIError(f"Branch '{branch}' not found", 404)
        handle_error_response(ref_response, f"{owner}/{repo}")

        ref_data = ref_response.json()
        latest_commit_sha = ref_data["object"]["sha"]

        # 2. Get the tree SHA for that commit
        commit_response = await client.get(
            f"{self.BASE_URL}/repos/{owner}/{repo}/git/commits/{latest_commit_sha}",
            headers=self._headers,
        )
        handle_error_response(commit_response, f"{owner}/{repo}")

        commit_data = commit_response.json()
        base_tree_sha = commit_data["tree"]["sha"]

        # 3. Create blobs for each file and build tree items
        tree_items = []
        for file in files:
            blob_response = await client.post(
                f"{self.BASE_URL}/repos/{owner}/{repo}/git/blobs",
                headers=self._headers,
                json={
                    "content": file["content"],
                    "encoding": "utf-8",
                },
            )

            if blob_response.status_code not in (200, 201):
                raise GitHubAPIError(
                    f"Failed to create blob for {file['path']}: {blob_response.status_code}",
                    blob_response.status_code,
                )

            blob_data = blob_response.json()
            tree_items.append(
                {
                    "path": file["path"],
                    "mode": "100644",
                    "type": "blob",
                    "sha": blob_data["sha"],
                }
            )

        # 4. Create a new tree
        tree_response = await client.post(
            f"{self.BASE_URL}/repos/{owner}/{repo}/git/trees",
            headers=self._headers,
            json={
                "base_tree": base_tree_sha,
                "tree": tree_items,
            },
        )

        if tree_response.status_code not in (200, 201):
            raise GitHubAPIError(
                f"Failed to create tree: {tree_response.status_code}",
                tree_response.status_code,
            )

        new_tree_sha = tree_response.json()["sha"]

        # 5. Create the commit
        commit_create_response = await client.post(
            f"{self.BASE_URL}/repos/{owner}/{repo}/git/commits",
            headers=self._headers,
            json={
                "message": message,
                "tree": new_tree_sha,
                "parents": [latest_commit_sha],
            },
        )

        if commit_create_response.status_code not in (200, 201):
            raise GitHubAPIError(
                f"Failed to create commit: {commit_create_response.status_code}",
                commit_create_response.status_code,
            )

        new_commit_sha = commit_create_response.json()["sha"]

        # 6. Update the branch reference
        ref_update_response = await client.patch(
            f"{self.BASE_URL}/repos/{owner}/{repo}/git/refs/heads/{branch}",
            headers=self._headers,
            json={"sha": new_commit_sha},
        )

        if ref_update_response.status_code == 422:
            raise GitHubAPIError(
                f"Branch '{branch}' is protected. Consider using a different "
                "sync branch or adjusting branch protection settings.",
                422,
            )

        if ref_update_response.status_code == 403:
            raise GitHubAPIError(
                f"Insufficient permissions to push to '{branch}' on {owner}/{repo}. "
                "Ensure the token has 'contents: write' permission.",
                403,
            )

        if ref_update_response.status_code != 200:
            raise GitHubAPIError(
                f"Failed to update branch ref: {ref_update_response.status_code}",
                ref_update_response.status_code,
            )

        result: str = new_commit_sha
        return result

    async def get_file_sha(
        self,
        owner: str,
        repo: str,
        path: str,
        branch: str = "main",
    ) -> str | None:
        """
        Get the SHA of a specific file in the repository.

        Args:
            owner: Repository owner
            repo: Repository name
            path: File path
            branch: Branch name (default: "main")

        Returns:
            File SHA or None if file doesn't exist
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

        sha: str | None = response.json().get("sha")
        return sha

    async def branch_exists(self, owner: str, repo: str, branch: str) -> bool:
        """
        Check whether a branch exists in the repository.

        Args:
            owner: Repository owner
            repo: Repository name
            branch: Branch name to check

        Returns:
            True if the branch exists, False otherwise
        """
        client = get_github_client()
        response = await client.get(
            f"{self.BASE_URL}/repos/{owner}/{repo}/git/refs/heads/{branch}",
            headers=self._headers,
        )

        if response.status_code == 404:
            return False

        if response.status_code == 200:
            return True

        # Unexpected status — let the standard handler raise
        handle_error_response(response, f"{owner}/{repo}")
        return False  # unreachable, but keeps mypy happy

    async def create_branch(
        self,
        owner: str,
        repo: str,
        branch: str,
        from_branch: str = "main",
    ) -> str:
        """
        Create a new branch from an existing branch.

        Idempotent: if the branch already exists, returns its name without error.

        Args:
            owner: Repository owner
            repo: Repository name
            branch: New branch name to create
            from_branch: Source branch to branch from (default: "main")

        Returns:
            The branch name (whether newly created or already existing)
        """
        # If branch already exists, return early
        if await self.branch_exists(owner, repo, branch):
            logger.info(f"Branch '{branch}' already exists on {owner}/{repo}")
            return branch

        client = get_github_client()

        # Get the SHA of the source branch's HEAD
        ref_response = await client.get(
            f"{self.BASE_URL}/repos/{owner}/{repo}/git/refs/heads/{from_branch}",
            headers=self._headers,
        )

        if ref_response.status_code == 404:
            raise GitHubAPIError(
                f"Source branch '{from_branch}' not found on {owner}/{repo}", 404
            )
        handle_error_response(ref_response, f"{owner}/{repo}")

        source_sha = ref_response.json()["object"]["sha"]

        # Create the new branch reference
        create_response = await client.post(
            f"{self.BASE_URL}/repos/{owner}/{repo}/git/refs",
            headers=self._headers,
            json={
                "ref": f"refs/heads/{branch}",
                "sha": source_sha,
            },
        )

        if create_response.status_code == 422:
            # Race condition: branch was created between our check and this call
            logger.info(f"Branch '{branch}' created concurrently on {owner}/{repo}")
            return branch

        if create_response.status_code == 403:
            raise GitHubAPIError(
                f"Insufficient permissions to create branch '{branch}' on {owner}/{repo}. "
                "Ensure the token has 'contents: write' permission.",
                403,
            )

        if create_response.status_code not in (200, 201):
            raise GitHubAPIError(
                f"Failed to create branch '{branch}': {create_response.status_code}",
                create_response.status_code,
            )

        logger.info(f"Created branch '{branch}' on {owner}/{repo} from '{from_branch}'")
        return branch

    async def create_pull_request(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str,
        head: str,
        base: str,
    ) -> PullRequestInfo:
        """
        Create a pull request, or return the existing one if a PR already
        exists for the same head → base combination.

        Args:
            owner: Repository owner
            repo: Repository name
            title: PR title
            body: PR description (markdown)
            head: Source branch name
            base: Target branch name

        Returns:
            PullRequestInfo with PR number, URLs, and state
        """
        client = get_github_client()

        create_response = await client.post(
            f"{self.BASE_URL}/repos/{owner}/{repo}/pulls",
            headers=self._headers,
            json={
                "title": title,
                "body": body,
                "head": head,
                "base": base,
            },
        )

        if create_response.status_code in (200, 201):
            data = create_response.json()
            return PullRequestInfo(
                number=data["number"],
                url=data["url"],
                html_url=data["html_url"],
                head=head,
                base=base,
                state=data["state"],
            )

        # 422 with "A pull request already exists" — find the existing PR
        if create_response.status_code == 422:
            return await self._find_existing_pull_request(owner, repo, head, base)

        if create_response.status_code == 403:
            raise GitHubAPIError(
                f"Insufficient permissions to create PR on {owner}/{repo}. "
                "Ensure the token has 'pull_requests: write' permission.",
                403,
            )

        raise GitHubAPIError(
            f"Failed to create pull request: {create_response.status_code}",
            create_response.status_code,
        )

    async def _find_existing_pull_request(
        self,
        owner: str,
        repo: str,
        head: str,
        base: str,
    ) -> PullRequestInfo:
        """
        Find an existing open pull request for the given head → base branches.

        Called when PR creation returns 422 (already exists).

        Raises:
            GitHubAPIError: If no matching open PR is found
        """
        client = get_github_client()

        # GitHub's pulls API accepts head as "owner:branch" or just "branch"
        search_response = await client.get(
            f"{self.BASE_URL}/repos/{owner}/{repo}/pulls",
            headers=self._headers,
            params={
                "state": "open",
                "head": f"{owner}:{head}",
                "base": base,
            },
        )

        handle_error_response(search_response, f"{owner}/{repo}")

        prs = search_response.json()
        if prs:
            data = prs[0]
            logger.info(
                f"Found existing PR #{data['number']} for {head} → {base} on {owner}/{repo}"
            )
            return PullRequestInfo(
                number=data["number"],
                url=data["url"],
                html_url=data["html_url"],
                head=head,
                base=base,
                state=data["state"],
            )

        raise GitHubAPIError(
            f"Pull request creation failed (422) and no existing open PR found "
            f"for {head} → {base} on {owner}/{repo}",
            422,
        )
