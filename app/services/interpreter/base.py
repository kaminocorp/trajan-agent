"""Base classes for message interpretation.

Provides abstract base interpreter and concrete implementation for
converting user messages into actionable tickets.
"""

from abc import ABC, abstractmethod
from typing import Generic, TypeVar

import anthropic

from app.config import settings

from .types import MessageInput, TicketOutput

TInput = TypeVar("TInput")
TOutput = TypeVar("TOutput")


class BaseInterpreter(ABC, Generic[TInput, TOutput]):
    """Abstract base for all message interpreters.

    Subclass this to create interpreters for different input sources
    or output formats. The base handles LLM communication; subclasses
    define prompts and parsing.
    """

    # Override in subclasses
    model: str = "claude-sonnet-4-6"
    max_tokens: int = 1000

    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or settings.anthropic_api_key
        self._client: anthropic.AsyncAnthropic | None = None

    @property
    def client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            self._client = anthropic.AsyncAnthropic(api_key=self.api_key)
        return self._client

    @abstractmethod
    def get_system_prompt(self) -> str:
        """Return the system prompt for this interpreter."""
        ...

    @abstractmethod
    def format_input(self, input_data: TInput) -> str:
        """Convert typed input to prompt string."""
        ...

    @abstractmethod
    def parse_output(self, response_text: str) -> TOutput:
        """Parse LLM response into typed output."""
        ...

    async def interpret(
        self, input_data: TInput, *, model_override: str | None = None
    ) -> TOutput:
        """Main entry point: interpret input and return structured output.

        Args:
            input_data: Typed input for this interpreter.
            model_override: If provided, use this model instead of self.model.
                Avoids mutating shared singleton state for concurrency safety.
        """
        user_message = self.format_input(input_data)

        response = await self.client.messages.create(
            model=model_override or self.model,
            max_tokens=self.max_tokens,
            system=self.get_system_prompt(),
            messages=[{"role": "user", "content": user_message}],
        )

        # Extract text from the first content block
        first_block = response.content[0]
        response_text = first_block.text if hasattr(first_block, "text") else str(first_block)
        return self.parse_output(response_text)


class MessageToTicketInterpreter(BaseInterpreter[MessageInput, TicketOutput]):
    """Concrete interpreter: converts any MessageInput to a TicketOutput.

    This is the primary reusable interpreter. It takes generic message
    input and produces actionable ticket output.
    """

    max_tokens: int = 800

    def get_system_prompt(self) -> str:
        return """You are a technical product manager. Your job is to convert user messages into clear, actionable dev tickets.

RULES:
1. Write a 2-3 sentence summary that captures the core issue/request
2. Determine the ticket type: "bug" (something broken), "feature" (new capability), "task" (general work), "question" (needs clarification)
3. Assess priority: "critical" (system down), "high" (blocks users), "medium" (important but not urgent), "low" (nice to have)
4. Suggest 1-3 labels from common categories: ui, api, performance, security, documentation, testing, infrastructure
5. If the request mentions specific acceptance criteria or success conditions, list them

OUTPUT FORMAT (use exactly this structure):
SUMMARY: <your 2-3 sentence summary>
TYPE: <bug|feature|task|question>
PRIORITY: <critical|high|medium|low>
LABELS: <comma-separated list>
ACCEPTANCE_CRITERIA:
- <criterion 1>
- <criterion 2>
(leave blank if none mentioned)"""

    def format_input(self, input_data: MessageInput) -> str:
        parts = []

        if input_data.title:
            parts.append(f"Title: {input_data.title}")

        if input_data.metadata:
            meta_str = ", ".join(f"{k}: {v}" for k, v in input_data.metadata.items())
            parts.append(f"Metadata: {meta_str}")

        parts.append(f"Source: {input_data.source}")

        if input_data.source_url:
            parts.append(f"URL: {input_data.source_url}")

        parts.append(f"\nMessage:\n{input_data.content}")

        return "\n".join(parts)

    def parse_output(self, response_text: str) -> TicketOutput:
        """Parse the structured response into TicketOutput."""
        lines = response_text.strip().split("\n")

        summary = ""
        ticket_type = "task"
        priority = "medium"
        labels: list[str] = []
        criteria: list[str] = []

        in_criteria = False

        for line in lines:
            line = line.strip()

            if line.startswith("SUMMARY:"):
                summary = line.replace("SUMMARY:", "").strip()
                in_criteria = False
            elif line.startswith("TYPE:"):
                ticket_type = line.replace("TYPE:", "").strip().lower()
                in_criteria = False
            elif line.startswith("PRIORITY:"):
                priority = line.replace("PRIORITY:", "").strip().lower()
                in_criteria = False
            elif line.startswith("LABELS:"):
                label_str = line.replace("LABELS:", "").strip()
                labels = [lbl.strip() for lbl in label_str.split(",") if lbl.strip()]
                in_criteria = False
            elif line.startswith("ACCEPTANCE_CRITERIA:"):
                in_criteria = True
            elif in_criteria and line.startswith("- "):
                criteria.append(line[2:].strip())

        return TicketOutput(
            summary=summary or response_text[:500],  # Fallback to raw if parsing fails
            ticket_type=(
                ticket_type if ticket_type in ("bug", "feature", "task", "question") else "task"
            ),
            priority=(priority if priority in ("critical", "high", "medium", "low") else "medium"),
            suggested_labels=labels,
            acceptance_criteria=criteria,
            raw_response=response_text,
        )
