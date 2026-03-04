"""AI-powered progress summarizer using Claude.

Generates concise narrative summaries of development activity
for the Progress tab's Summary view. Also generates per-contributor
summaries for email digest progress review sections.
"""

from dataclasses import dataclass

from app.services.interpreter.base import BaseInterpreter


@dataclass
class ProgressData:
    """Input data for progress summary generation.

    Contains aggregated statistics and highlights from the progress API.
    """

    period: str  # e.g., "7d", "30d"
    total_commits: int
    total_contributors: int
    total_additions: int
    total_deletions: int
    focus_areas: list[dict[str, str | int]]  # [{path: str, commits: int}, ...]
    top_contributors: list[dict[str, str | int]]  # [{author: str, commits: int}, ...]
    recent_commits: list[dict[str, str]]  # [{message, author, sha?, branch?}, ...]


@dataclass
class ProgressNarrative:
    """Output from progress summary generation."""

    summary: str  # 2-4 sentence narrative


class ProgressSummarizer(BaseInterpreter[ProgressData, ProgressNarrative]):
    """Generates concise narrative summaries of development progress.

    Uses Claude to synthesize commit statistics, focus areas, and contributor
    activity into a PM-friendly 2-4 sentence update.
    """

    model: str = "claude-sonnet-4-6"
    max_tokens: int = 300

    def get_system_prompt(self) -> str:
        return """You are a concise technical communicator summarizing development activity for product managers and stakeholders.

TASK: Write a 2-4 sentence summary of the development activity. Be specific about what was accomplished.

STYLE:
- Lead with the most impactful changes or achievements
- Mention specific focus areas if activity is concentrated
- Note notable contributor activity only if relevant
- Use active voice and concrete language
- Avoid generic phrases like "various improvements" or "multiple changes"
- When commit SHAs and branch names are provided, weave them in naturally as inline references (e.g., "Added OAuth support [abc1234, feature/oauth]"). Include 1-3 key refs, not every commit.

OUTPUT: Write ONLY the summary text. No labels, no bullet points, no formatting. Just 2-4 natural sentences."""

    def format_input(self, input_data: ProgressData) -> str:
        """Format progress data into a structured prompt."""
        lines = [
            f"Development activity for the past {self._period_to_text(input_data.period)}:",
            "",
            "STATS:",
            f"- {input_data.total_commits} commits by {input_data.total_contributors} contributor(s)",
            f"- {input_data.total_additions:,} lines added, {input_data.total_deletions:,} lines removed",
            "",
        ]

        if input_data.focus_areas:
            lines.append("FOCUS AREAS (by commit count):")
            for area in input_data.focus_areas[:5]:
                lines.append(f"- {area['path']}: {area['commits']} commits")
            lines.append("")

        if input_data.top_contributors:
            lines.append("TOP CONTRIBUTORS:")
            for contrib in input_data.top_contributors[:3]:
                lines.append(f"- {contrib['author']}: {contrib['commits']} commits")
            lines.append("")

        if input_data.recent_commits:
            lines.append("RECENT COMMITS:")
            for commit in input_data.recent_commits[:5]:
                # Truncate long messages
                msg = commit.get("message", "")[:80]
                author = commit.get("author", "Unknown")
                sha = commit.get("sha", "")[:7]
                branch = commit.get("branch", "")
                ref_parts = [p for p in [sha, branch] if p]
                ref_str = f" [{', '.join(ref_parts)}]" if ref_parts else ""
                lines.append(f'- "{msg}" ({author}){ref_str}')

        return "\n".join(lines)

    def parse_output(self, response_text: str) -> ProgressNarrative:
        """Extract the summary text from the response."""
        # The prompt asks for plain text only, so minimal parsing needed
        summary = response_text.strip()

        # Remove any accidental prefixes the model might add
        prefixes_to_remove = ["Summary:", "SUMMARY:", "Here's the summary:", "Here is the summary:"]
        for prefix in prefixes_to_remove:
            if summary.startswith(prefix):
                summary = summary[len(prefix) :].strip()

        return ProgressNarrative(summary=summary)

    def _period_to_text(self, period: str) -> str:
        """Convert period code to human-readable text."""
        period_map = {
            "24h": "24 hours",
            "48h": "48 hours",
            "7d": "7 days",
            "14d": "14 days",
            "30d": "30 days",
            "90d": "90 days",
            "365d": "year",
        }
        return period_map.get(period, period)


# Singleton instance
progress_summarizer = ProgressSummarizer()


# ---------------------------------------------------------------------------
# Per-contributor summary generation
# ---------------------------------------------------------------------------


@dataclass
class ContributorCommitData:
    """Commit data for a single contributor."""

    name: str
    commits: list[dict[str, str]]  # [{message, sha, branch?, timestamp}, ...]
    commit_count: int = 0
    additions: int = 0
    deletions: int = 0


@dataclass
class ContributorInput:
    """Input for contributor summary generation — all contributors for one product."""

    period: str
    product_name: str
    contributors: list[ContributorCommitData]


@dataclass
class ContributorSummaryItem:
    """AI-generated summary for a single contributor."""

    name: str
    summary_text: str
    commit_count: int
    additions: int
    deletions: int
    commit_refs: list[dict[str, str]]  # [{sha, branch}, ...]


@dataclass
class ContributorSummaries:
    """Output: list of per-contributor summaries."""

    items: list[ContributorSummaryItem]


class ContributorSummarizer(BaseInterpreter[ContributorInput, ContributorSummaries]):
    """Generates per-contributor AI summaries for email digest progress reviews.

    Takes commits grouped by contributor and produces 2-3 sentence summaries
    per contributor with commit SHA/branch references. Used in daily/weekly
    email digests to show who shipped what.
    """

    model: str = "claude-sonnet-4-6"
    max_tokens: int = 800

    def get_system_prompt(self) -> str:
        return """You are a concise technical communicator summarizing individual developer contributions for a progress digest email.

TASK: For each contributor listed, write a 2-3 sentence summary of what they worked on, referencing specific commit SHAs in square brackets.

STYLE:
- Use active voice: "Implemented X", "Fixed Y", "Refactored Z"
- Be specific about what was accomplished, not generic
- Include 1-3 commit SHA refs per contributor in [sha] format
- Focus on functional outcomes, not commit count

OUTPUT FORMAT (one block per contributor, separated by blank lines):
CONTRIBUTOR: <name>
<2-3 sentence summary with [sha] references>

Example:
CONTRIBUTOR: Alice
Implemented the OAuth login flow with Google and GitHub providers [a1b2c3d]. Also fixed a session timeout bug that affected long-running connections [e4f5g6h]."""

    def format_input(self, input_data: ContributorInput) -> str:
        """Format per-contributor commit data into a prompt."""
        lines = [
            f"Project: {input_data.product_name}",
            f"Period: Last {self._period_to_text(input_data.period)}",
            "",
        ]

        # Cap at 5 contributors to keep token usage reasonable
        for contrib in input_data.contributors[:5]:
            lines.append(f"--- {contrib.name} ({contrib.commit_count} commits) ---")
            for commit in contrib.commits[:10]:
                sha = commit.get("sha", "")[:7]
                msg = commit.get("message", "")[:100]
                branch = commit.get("branch", "")
                branch_str = f" ({branch})" if branch else ""
                lines.append(f"  [{sha}] {msg}{branch_str}")
            if len(contrib.commits) > 10:
                lines.append(f"  ... and {len(contrib.commits) - 10} more commits")
            lines.append("")

        return "\n".join(lines)

    def parse_output(self, response_text: str) -> ContributorSummaries:
        """Parse per-contributor summaries from the AI response."""
        items: list[ContributorSummaryItem] = []
        lines = response_text.strip().split("\n")

        current_name: str | None = None
        current_summary_lines: list[str] = []

        for line in lines:
            stripped = line.strip()

            if stripped.startswith("CONTRIBUTOR:"):
                # Flush previous contributor
                if current_name and current_summary_lines:
                    items.append(self._build_item(current_name, current_summary_lines))

                current_name = stripped.replace("CONTRIBUTOR:", "").strip()
                current_summary_lines = []
            elif current_name and stripped:
                current_summary_lines.append(stripped)

        # Flush last contributor
        if current_name and current_summary_lines:
            items.append(self._build_item(current_name, current_summary_lines))

        return ContributorSummaries(items=items)

    def _build_item(self, name: str, summary_lines: list[str]) -> ContributorSummaryItem:
        """Build a ContributorSummaryItem from parsed lines.

        Extracts commit refs from [sha] patterns in the summary text.
        Stats (commit_count, additions, deletions) are populated later
        by the caller from the actual commit data.
        """
        import re

        summary_text = " ".join(summary_lines)
        # Extract [sha] refs from the summary text
        sha_refs = re.findall(r"\[([a-f0-9]{7,})\]", summary_text)
        commit_refs = [{"sha": sha, "branch": ""} for sha in sha_refs]

        return ContributorSummaryItem(
            name=name,
            summary_text=summary_text,
            commit_count=0,  # Populated by caller
            additions=0,
            deletions=0,
            commit_refs=commit_refs,
        )

    def _period_to_text(self, period: str) -> str:
        """Convert period code to human-readable text."""
        period_map = {
            "24h": "24 hours",
            "1d": "24 hours",
            "48h": "48 hours",
            "7d": "7 days",
            "14d": "14 days",
            "30d": "30 days",
            "90d": "90 days",
            "365d": "year",
        }
        return period_map.get(period, period)

    async def interpret(
        self, input_data: ContributorInput, *, model_override: str | None = None
    ) -> ContributorSummaries:
        """Override to handle empty contributors without AI call."""
        if not input_data.contributors:
            return ContributorSummaries(items=[])

        self._current_input = input_data
        result = await super().interpret(input_data, model_override=model_override)

        # Backfill stats from the input data
        contrib_map = {c.name: c for c in input_data.contributors}
        for item in result.items:
            source = contrib_map.get(item.name)
            if source:
                item.commit_count = source.commit_count
                item.additions = source.additions
                item.deletions = source.deletions
                # Enrich commit_refs with branch info from source commits
                sha_to_branch = {c.get("sha", "")[:7]: c.get("branch", "") for c in source.commits}
                for ref in item.commit_refs:
                    ref["branch"] = sha_to_branch.get(ref["sha"], "")

        return result


# Singleton instance
contributor_summarizer = ContributorSummarizer()
