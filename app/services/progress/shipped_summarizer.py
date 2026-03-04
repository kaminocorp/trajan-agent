"""AI-powered shipped summarizer using Claude.

Generates outcome-focused summaries of what was functionally shipped
for the Dashboard Progress section. Unlike ProgressSummarizer which
focuses on activity metrics, this emphasizes user-facing outcomes.
"""

from dataclasses import dataclass, field
from uuid import UUID

from app.services.interpreter.base import BaseInterpreter


@dataclass
class CommitInfo:
    """Information about a single commit for shipped analysis."""

    sha: str
    message: str
    author: str
    timestamp: str
    files: list[str] = field(default_factory=list)  # File paths changed


@dataclass
class ShippedAnalysisInput:
    """Input data for shipped summary generation.

    Contains commit data for a single product over a time period.
    """

    product_id: UUID
    product_name: str
    period: str  # e.g., "7d", "14d", "30d"
    commits: list[CommitInfo]


@dataclass
class ShippedItem:
    """A single item that was shipped."""

    description: str  # e.g., "Added user authentication flow with OAuth support"
    category: str  # "feature" | "fix" | "improvement" | "refactor"


@dataclass
class ShippedSummary:
    """Output from shipped summary generation."""

    product_id: UUID
    product_name: str
    items: list[ShippedItem]
    has_significant_changes: bool


class ShippedSummarizer(BaseInterpreter[ShippedAnalysisInput, ShippedSummary]):
    """Generates outcome-focused summaries of what shipped.

    Uses Claude to analyze commit messages and file changes to identify
    what was functionally/materially shipped - features, fixes, improvements.
    Filters out noise like dependency updates, linting, or trivial changes.
    """

    model: str = "claude-sonnet-4-6"
    max_tokens: int = 500

    def get_system_prompt(self) -> str:
        return """You are a technical communicator who summarizes development work for product managers and stakeholders.

TASK: Analyze the commit messages and identify what was FUNCTIONALLY SHIPPED - features added, bugs fixed, or meaningful improvements made.

RULES:
1. Focus on USER-FACING or FUNCTIONAL changes only
2. IGNORE trivial changes: dependency updates, linting fixes, refactors without user impact, config changes
3. Group related commits into single items (e.g., multiple commits for "login feature" = 1 item)
4. Use active voice: "Added X", "Fixed Y", "Improved Z"
5. Be concise: max 3-5 items, each 5-15 words
6. If nothing significant was shipped, output NO_SIGNIFICANT_CHANGES
7. Include 1-2 commit SHA short refs per item in square brackets at the end, referencing the most relevant commits for that item

OUTPUT FORMAT (use exactly this structure):
ITEM: feature | <description> [<sha>, ...]
ITEM: fix | <description> [<sha>]
ITEM: improvement | <description> [<sha>, <sha>]

Categories:
- feature: New functionality or capability
- fix: Bug fix or error correction
- improvement: Enhancement to existing functionality
- refactor: Structural change with user-visible benefit (rare)

If no significant changes:
NO_SIGNIFICANT_CHANGES

Example output:
ITEM: feature | Added user authentication flow with OAuth support [a1b2c3d, e4f5g6h]
ITEM: fix | Fixed login redirect bug affecting Safari users [i7j8k9l]
ITEM: improvement | Improved search performance with indexed queries [m0n1o2p]"""

    def format_input(self, input_data: ShippedAnalysisInput) -> str:
        """Format commit data into a prompt for the AI."""
        if not input_data.commits:
            return f"Project: {input_data.product_name}\nPeriod: {input_data.period}\n\nNo commits in this period."

        lines = [
            f"Project: {input_data.product_name}",
            f"Period: Last {self._period_to_text(input_data.period)}",
            f"Total commits: {len(input_data.commits)}",
            "",
            "COMMITS:",
        ]

        # Group commits by author for context, but limit to avoid token bloat
        max_commits = 50  # Cap input to avoid expensive API calls
        commits_to_analyze = input_data.commits[:max_commits]

        for commit in commits_to_analyze:
            # Truncate long messages
            message = commit.message[:120]
            if len(commit.message) > 120:
                message += "..."

            sha_short = commit.sha[:7] if commit.sha else ""
            lines.append(f"- [{sha_short}] {message} ({commit.author})")

            # Include top 5 file paths if available (helps identify scope)
            if commit.files:
                file_sample = commit.files[:5]
                files_str = ", ".join(file_sample)
                if len(commit.files) > 5:
                    files_str += f" (+{len(commit.files) - 5} more)"
                lines.append(f"  Files: {files_str}")

        if len(input_data.commits) > max_commits:
            lines.append(f"\n(Showing {max_commits} of {len(input_data.commits)} commits)")

        return "\n".join(lines)

    def parse_output(self, response_text: str) -> ShippedSummary:
        """Parse the AI response into ShippedSummary."""
        lines = response_text.strip().split("\n")

        items: list[ShippedItem] = []
        has_significant_changes = True

        for line in lines:
            line = line.strip()

            if line == "NO_SIGNIFICANT_CHANGES":
                has_significant_changes = False
                break

            if line.startswith("ITEM:"):
                # Parse: "ITEM: category | description"
                content = line.replace("ITEM:", "").strip()
                parts = content.split("|", 1)

                if len(parts) == 2:
                    category = parts[0].strip().lower()
                    description = parts[1].strip()

                    # Validate category
                    valid_categories = ("feature", "fix", "improvement", "refactor")
                    if category not in valid_categories:
                        category = "improvement"  # Default fallback

                    items.append(ShippedItem(description=description, category=category))

        # If we got items, we have significant changes
        if items:
            has_significant_changes = True

        return ShippedSummary(
            product_id=self._current_input.product_id,
            product_name=self._current_input.product_name,
            items=items,
            has_significant_changes=has_significant_changes,
        )

    async def interpret(
        self, input_data: ShippedAnalysisInput, *, model_override: str | None = None
    ) -> ShippedSummary:
        """Override to store input data for use in parse_output."""
        # Store input for access in parse_output (needed for product_id/name)
        self._current_input = input_data

        # Handle empty commits case without calling AI
        if not input_data.commits:
            return ShippedSummary(
                product_id=input_data.product_id,
                product_name=input_data.product_name,
                items=[],
                has_significant_changes=False,
            )

        return await super().interpret(input_data, model_override=model_override)

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
shipped_summarizer = ShippedSummarizer()
