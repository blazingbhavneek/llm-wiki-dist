"""Small base interface for chat clients."""

from __future__ import annotations

from typing import Any


class BaseLlmClient:
    """Minimal shape shared by chat clients in this repo."""

    message_history: list[Any]

    def invoke(self, prompt: str) -> str:
        raise NotImplementedError

    def invoke_structured(self, prompt: str, output_model: type[Any]) -> Any:
        raise NotImplementedError

    def run_messages(self, messages: list[Any]) -> str:
        raise NotImplementedError

    def run_messages_structured(
        self,
        messages: list[Any],
        output_model: type[Any],
    ) -> Any:
        raise NotImplementedError

    def complete(self, system_prompt: str, user_content: str) -> str:
        """One-shot system+user completion returning text. No shared history."""
        raise NotImplementedError

    def complete_structured(
        self,
        system_prompt: str,
        user_content: str,
        output_model: type[Any],
    ) -> Any:
        """One-shot system+user completion parsed into ``output_model``."""
        raise NotImplementedError

    def reset_history(self) -> None:
        raise NotImplementedError
