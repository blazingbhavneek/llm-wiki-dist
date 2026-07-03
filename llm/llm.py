"""Shared NVIDIA chat client with persistent history and retryable queries."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import requests
from pydantic import ValidationError

from .base import BaseLlmClient
from .utils import strip_image_media

INVOKE_URL = "http://10.160.144.101:51026/v1"
API_KEY = (
    "<API_KEY>"
)
MODEL = "openai/gpt-oss-120b"



class LlmClient(BaseLlmClient):
    """Small chat wrapper with persistent history and simple retries."""

    def __init__(
        self,
        model: str,
        base_url: str,
        api_key: str,
        *,
        system_prompt: str = "",
        temperature: float = 0.0,
        timeout: int = 300,
        retry_attempts: int = 3,
        retry_delay_seconds: float = 1.0,
    ) -> None:
        self.model = model or MODEL
        self.base_url = self._normalize_invoke_url(base_url or INVOKE_URL)
        self.api_key = api_key or API_KEY
        self.system_prompt = strip_image_media(system_prompt)
        self.temperature = temperature
        self.timeout = timeout
        self.retry_attempts = max(0, retry_attempts)
        self.retry_delay_seconds = max(0.0, retry_delay_seconds)
        self.message_history: list[dict[str, Any]] = []
        if self.system_prompt.strip():
            self.message_history.append(
                {"role": "system", "content": self.system_prompt.strip()}
            )

    def invoke(self, prompt: str) -> str:
        prompt = strip_image_media(prompt).strip()
        if not prompt:
            raise ValueError("prompt must not be empty")

        user_message = {"role": "user", "content": prompt}
        messages = [*self.message_history, user_message]
        reply = self.run_messages(messages)
        self.message_history.append(user_message)
        self.message_history.append({"role": "assistant", "content": reply})
        return reply

    def invoke_structured(self, prompt: str, output_model: type[Any]) -> Any:
        prompt = strip_image_media(prompt).strip()
        if not prompt:
            raise ValueError("prompt must not be empty")

        user_message = {"role": "user", "content": prompt}
        messages = [*self.message_history, user_message]
        result = self.run_messages_structured(messages, output_model)
        self.message_history.append(user_message)
        self.message_history.append(
            {"role": "assistant", "content": self._stringify_output(result)}
        )
        return result

    def run_messages(self, messages: list[Any]) -> str:
        return self._run_with_retries(
            lambda: self._response_text(
                self._chat_completion(self._normalize_messages(messages))
            )
        )

    def run_messages_structured(
        self,
        messages: list[Any],
        output_model: type[Any],
    ) -> Any:
        normalized = self._normalize_messages(messages)
        schema = output_model.model_json_schema()
        constrained = self._with_json_schema_instruction(normalized, schema)
        return self._run_with_retries(
            lambda: self._parse_structured_response(
                self._chat_completion(constrained),
                output_model,
            )
        )

    def complete(self, system_prompt: str, user_content: str) -> str:
        return self.run_messages(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ]
        )

    def complete_structured(
        self,
        system_prompt: str,
        user_content: str,
        output_model: type[Any],
    ) -> Any:
        return self.run_messages_structured(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            output_model,
        )

    def reset_history(self) -> None:
        self.message_history = []
        if self.system_prompt.strip():
            self.message_history.append(
                {"role": "system", "content": self.system_prompt.strip()}
            )

    def _chat_completion(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        enable_thinking: bool = True,
        temperature: float | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "stream": False,
            "chat_template_kwargs": {"enable_thinking": enable_thinking},
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        response = requests.post(
            self.base_url,
            headers=self._headers(),
            json=payload,
            timeout=self.timeout,
        )
        response.raise_for_status()
        body = response.json()
        message = self._first_message(body)
        if not isinstance(message, dict):
            raise RuntimeError("chat completions response did not include a message")
        return message

    def _normalize_messages(self, messages: list[Any]) -> list[dict[str, Any]]:
        normalized: list[dict[str, Any]] = []
        for message in messages:
            if isinstance(message, dict):
                entry = dict(message)
                if isinstance(entry.get("content"), str):
                    entry["content"] = strip_image_media(entry["content"])
                normalized.append(entry)
                continue

            role = getattr(message, "type", None) or getattr(message, "role", None)
            content = getattr(message, "content", None)
            if role == "human":
                role = "user"
            elif role == "ai":
                role = "assistant"

            if not role or content is None:
                raise TypeError(f"Unsupported message type: {type(message)!r}")

            if isinstance(content, str):
                content = strip_image_media(content)

            entry: dict[str, Any] = {"role": role, "content": content}
            tool_call_id = getattr(message, "tool_call_id", None)
            if tool_call_id:
                entry["tool_call_id"] = tool_call_id
            normalized.append(entry)
        return normalized

    def _with_json_schema_instruction(
        self,
        messages: list[dict[str, Any]],
        schema: dict[str, Any],
    ) -> list[dict[str, Any]]:
        instruction = (
            "Return only valid JSON matching this schema exactly.\n"
            f"{json.dumps(schema, ensure_ascii=False)}"
        )
        updated = [dict(message) for message in messages]
        if updated and updated[0].get("role") == "system":
            content = str(updated[0].get("content", "")).strip()
            updated[0]["content"] = (
                f"{content}\n\n{instruction}" if content else instruction
            )
            return updated

        return [{"role": "system", "content": instruction}, *updated]

    def _parse_structured_response(
        self,
        message: dict[str, Any],
        output_model: type[Any],
    ) -> Any:
        raw = self._response_text(message)
        try:
            return output_model.model_validate_json(self._extract_json_text(raw))
        except (ValidationError, ValueError) as exc:
            raise RuntimeError(
                f"structured response validation failed: {exc}\nraw: {raw}"
            ) from exc

    def _extract_json_text(self, text: str) -> str:
        text = text.strip()
        if text.startswith("```"):
            parts = text.split("```")
            if len(parts) >= 3:
                text = parts[1]
                if text.startswith("json"):
                    text = text[4:]
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end >= start:
            return text[start : end + 1]
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1 and end >= start:
            return text[start : end + 1]
        raise ValueError("no JSON object or array found in model response")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _normalize_invoke_url(self, value: str) -> str:
        base = (value or INVOKE_URL).rstrip("/")
        if base.endswith("/chat/completions"):
            return base
        if base.endswith("/v1"):
            return f"{base}/chat/completions"
        return f"{base}/v1/chat/completions"

    def _run_with_retries(self, fn) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.retry_attempts + 1):
            try:
                return fn()
            except Exception as exc:
                last_error = exc
                if attempt >= self.retry_attempts:
                    break
                time.sleep(self.retry_delay_seconds)
        assert last_error is not None
        raise last_error

    def _first_message(self, response_body: dict[str, Any]) -> dict[str, Any]:
        choices = response_body.get("choices")
        if not isinstance(choices, list) or not choices:
            raise RuntimeError(
                f"chat completions response missing choices: {response_body}"
            )
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise RuntimeError(
                f"chat completions response missing message: {response_body}"
            )
        return message

    def _response_text(self, response: Any) -> str:
        content = (
            response.get("content", response)
            if isinstance(response, dict)
            else response
        )
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                else:
                    parts.append(str(item))
            return "\n".join(part for part in parts if part).strip()
        return str(content or "").strip()

    def _stringify_output(self, result: Any) -> str:
        if hasattr(result, "model_dump_json"):
            return result.model_dump_json()
        if isinstance(result, (dict, list)):
            return json.dumps(result, ensure_ascii=False)
        return str(result)

    def _tool_schema(self, tool: Any) -> dict[str, Any]:
        schema = tool.model_json_schema()
        properties = schema.get("properties", {})
        description = (tool.__doc__ or "").strip()
        return {
            "type": "function",
            "function": {
                "name": tool.__name__,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": schema.get("required", []),
                },
            },
        }

    def _assistant_message_with_tools(self, message: dict[str, Any]) -> dict[str, Any]:
        content = message.get("content", "")
        assistant: dict[str, Any] = {"role": "assistant", "content": content}
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            assistant["tool_calls"] = tool_calls
        return assistant

    def _tool_result_message(
        self, tool_call_id: str, observation: str
    ) -> dict[str, Any]:
        return {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": observation,
        }

    def _parse_tool_args(self, raw_args: Any) -> dict[str, Any]:
        if isinstance(raw_args, dict):
            return raw_args
        if isinstance(raw_args, str):
            try:
                parsed = json.loads(raw_args)
            except ValueError:
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    def _make_tool_call_id(self) -> str:
        return f"call_{uuid.uuid4().hex}"
