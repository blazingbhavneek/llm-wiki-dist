"""Tool-using LLM client.

`AgentClient` extends `LlmClient` with native tool-calling: it binds tool schemas
to the model and runs a bounded reason-act loop, dispatching each tool call back
to a caller-supplied callback. It is the only place that knows the LLM library's
tool-call message shape; callers receive a neutral `ToolLoopResult`.
"""

from __future__ import annotations

from typing import Any, Callable

from pydantic import BaseModel

from .llm import LlmClient
from .utils import strip_image_media


class ToolLoopResult(BaseModel):
    """Neutral result of a tool loop. No LLM-library types cross this boundary."""

    finished_args: dict[str, Any] | None = (
        None  # args of the terminal finish tool, if called
    )
    content: str = ""  # free-text answer when the model stops without finish
    steps: int = 0


class AgentClient(LlmClient):
    """LLM client that can bind tools and drive a reason-act loop."""

    def run_tools(self, messages: list[Any], tools: list[Any]) -> Any:
        """Send one tool-enabled chat completion and return the assistant message."""
        normalized = self._normalize_messages(messages)
        schemas = [self._tool_schema(tool) for tool in tools]
        return self._run_with_retries(
            lambda: self._chat_completion(normalized, tools=schemas)
        )

    def run_tool_loop(
        self,
        system_prompt: str,
        user_prompt: str,
        tools: list[Any],
        dispatch: Callable[[str, dict[str, Any]], str],
        max_steps: int,
        finish_tool: str = "finish",
        finish_guard: Callable[[dict[str, Any]], str | None] | None = None,
    ) -> ToolLoopResult:
        """Loop: model picks tools, `dispatch` runs them, until finish/no-call/cap.

        `dispatch(name, args) -> observation_text` is supplied by the caller and is
        the only domain-aware part; this client stays domain-agnostic.

        `finish_guard(args) -> reason | None` (optional) can VETO an early finish:
        if it returns a reason string, finish is rejected, the reason is fed back
        as a tool observation, and the loop continues (still bounded by max_steps).
        """
        messages: list[Any] = [
            {"role": "system", "content": strip_image_media(system_prompt)},
            {"role": "user", "content": strip_image_media(user_prompt)},
        ]
        steps = 0
        for _ in range(max(1, max_steps)):
            steps += 1
            ai = self.run_tools(messages, tools)
            messages.append(self._assistant_message_with_tools(ai))
            tool_calls = getattr(ai, "tool_calls", None) or []
            if isinstance(ai, dict):
                tool_calls = ai.get("tool_calls") or tool_calls
            if not tool_calls:
                return ToolLoopResult(content=self._response_text(ai), steps=steps)
            finished: dict[str, Any] | None = None
            for call in tool_calls:
                function = call.get("function") or {}
                name = function.get("name") or call.get("name")
                args = self._parse_tool_args(
                    function.get("arguments", call.get("args"))
                )
                tool_call_id = call.get("id") or self._make_tool_call_id()
                if name == finish_tool:
                    reason = finish_guard(args) if finish_guard else None
                    if reason:
                        messages.append(self._tool_result_message(tool_call_id, reason))
                        continue
                    finished = args
                    break
                observation = dispatch(name, args)
                messages.append(self._tool_result_message(tool_call_id, observation))
            if finished is not None:
                return ToolLoopResult(finished_args=finished, steps=steps)

        return ToolLoopResult(content="", steps=steps)
