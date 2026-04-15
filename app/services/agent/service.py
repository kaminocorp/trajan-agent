"""CLI Agent service for conversational project queries."""

import logging
import uuid as uuid_pkg
from collections.abc import AsyncIterator
from typing import Any, Literal, cast

import anthropic
from anthropic.types import MessageParam, ToolResultBlockParam
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.domain import repository_ops

from .context import ContextBuilder
from .prompts import AGENT_SYSTEM_PROMPT
from .tools import AGENT_TOOLS, CODE_GRAPH_TOOLS, AgentToolExecutor

logger = logging.getLogger(__name__)

# Max agentic tool-use iterations per message
_MAX_TOOL_ITERATIONS = 5


class CLIAgentService:
    """Conversational agent for project queries.

    Unlike BaseInterpreter (single-shot), this handles multi-turn
    conversations by accepting full message history. When GitHub is
    connected, the agent can also use tools (read_file, list_files)
    to fetch specific files on demand during the conversation.
    """

    model: str = "claude-sonnet-4-6"
    max_tokens: int = 2048

    def __init__(self, api_key: str | None = None) -> None:
        self.api_key = api_key or settings.anthropic_api_key
        self._client: anthropic.AsyncAnthropic | None = None
        self._context_builder = ContextBuilder()

    @property
    def client(self) -> anthropic.AsyncAnthropic:
        """Lazy-loaded async client (same pattern as BaseInterpreter)."""
        if self._client is None:
            self._client = anthropic.AsyncAnthropic(api_key=self.api_key)
        return self._client

    async def chat(
        self,
        db: AsyncSession,
        product_id: uuid_pkg.UUID,
        messages: list[dict[str, str]],
        github_token: str | None = None,
    ) -> str:
        """Send a conversational message with product context.

        Args:
            db: Database session with RLS context set.
            product_id: The product to query about.
            messages: Full conversation history [{role, content}, ...].
            github_token: Optional GitHub token for live repo context.

        Returns:
            The assistant's response text.
        """
        context = await self._context_builder.build(db, product_id, github_token=github_token)
        system = f"{AGENT_SYSTEM_PROMPT}\n\n---\n\n{context}"

        loop_messages: list[MessageParam] = [
            {
                "role": cast(Literal["user", "assistant"], m["role"]),
                "content": m["content"],
            }
            for m in messages
        ]

        # Set up tools if GitHub is connected
        tools_kwargs, executor = await self._prepare_tools(db, product_id, github_token)

        # Agentic loop — iterate until the model produces a text-only response
        for _ in range(_MAX_TOOL_ITERATIONS + 1):
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=loop_messages,
                **tools_kwargs,
            )

            # Check for tool use
            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
            if not tool_use_blocks or not executor:
                # No tool use — extract text and return
                text_blocks = [b for b in response.content if hasattr(b, "text")]
                return text_blocks[0].text if text_blocks else ""

            # Execute tools and continue the loop
            await self._handle_tool_use(loop_messages, response, tool_use_blocks, executor)

        return (
            "I've reached the maximum number of file lookups for this message. "
            "Please ask a follow-up question for more details."
        )

    async def chat_stream(
        self,
        db: AsyncSession,
        product_id: uuid_pkg.UUID,
        messages: list[dict[str, str]],
        github_token: str | None = None,
    ) -> AsyncIterator[str]:
        """Stream a conversational response, yielding text deltas.

        When the model uses tools, text from intermediate responses is streamed
        normally (e.g. "Let me check that file..."), tools are executed during
        the pause, and the continuation is streamed in the next iteration.
        """
        context = await self._context_builder.build(db, product_id, github_token=github_token)
        system = f"{AGENT_SYSTEM_PROMPT}\n\n---\n\n{context}"

        loop_messages: list[MessageParam] = [
            {
                "role": cast(Literal["user", "assistant"], m["role"]),
                "content": m["content"],
            }
            for m in messages
        ]

        # Set up tools if GitHub is connected
        tools_kwargs, executor = await self._prepare_tools(db, product_id, github_token)

        # Agentic loop with streaming
        for _ in range(_MAX_TOOL_ITERATIONS + 1):
            async with self.client.messages.stream(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                messages=loop_messages,
                **tools_kwargs,
            ) as stream:
                async for text in stream.text_stream:
                    yield text
                response = await stream.get_final_message()

            # Check for tool use
            tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
            if not tool_use_blocks or not executor:
                break

            # Execute tools and continue the loop
            await self._handle_tool_use(loop_messages, response, tool_use_blocks, executor)

    async def _prepare_tools(
        self,
        db: AsyncSession,
        product_id: uuid_pkg.UUID,
        github_token: str | None,
    ) -> tuple[dict[str, Any], AgentToolExecutor | None]:
        """Set up tool definitions and executor if GitHub is connected.

        Includes code graph tools when at least one repo has been indexed.

        Returns:
            Tuple of (kwargs dict to spread into API call, executor or None).
        """
        if not github_token:
            return {}, None

        repos = await repository_ops.get_by_product(db, product_id, limit=50)
        if not repos:
            return {}, None

        # Check if any repos have been indexed
        has_indexed = any(
            getattr(r, "indexing_status", None) == "completed" for r in repos
        )

        tools = list(AGENT_TOOLS)
        if has_indexed:
            tools.extend(CODE_GRAPH_TOOLS)

        executor = AgentToolExecutor(github_token, repos[:3], db=db if has_indexed else None)
        return {"tools": tools}, executor

    @staticmethod
    async def _handle_tool_use(
        loop_messages: list[MessageParam],
        response: anthropic.types.Message,
        tool_use_blocks: list[Any],
        executor: AgentToolExecutor,
    ) -> None:
        """Execute tool calls and append results to the message list.

        Modifies loop_messages in place by appending the assistant's
        tool-use response and the corresponding tool results.
        """
        # Append the assistant's response (contains both text and tool_use blocks)
        loop_messages.append({"role": "assistant", "content": response.content})

        # Execute each tool and collect results
        tool_results: list[ToolResultBlockParam] = []
        for block in tool_use_blocks:
            result = await executor.execute(
                block.name,
                block.input,
            )
            logger.debug("Tool %s executed (result: %d chars)", block.name, len(result))
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                }
            )

        # Append tool results as a user message (Anthropic API convention)
        loop_messages.append({"role": "user", "content": tool_results})
