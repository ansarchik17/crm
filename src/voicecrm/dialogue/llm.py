import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import httpx

from voicecrm.config import Settings
from voicecrm.dialogue.callback_tools import (
    CALLBACK_DRAFT_REPLY,
    CALLBACK_RESPONSE_FORMAT,
    CALLBACK_SYSTEM_PROMPT,
    CALLBACK_TOOLS,
    parse_callback_arguments,
    parse_callback_reply,
)
from voicecrm.dialogue.prompt import SYSTEM_PROMPT
from voicecrm.errors import ProviderError


ToolExecutor = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class LanguageModel(Protocol):
    async def reply(self, messages: list[dict[str, str]]) -> str: ...


class DemoModel:
    async def reply(self, messages: list[dict[str, str]]) -> str:
        return "Это тестовый режим без AI. Сообщение принято. Подключите OpenAI для проверки диалога."


class OpenAIModel:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client

    async def reply(self, messages: list[dict[str, str]]) -> str:
        api_key = self.settings.openai_api_key.get_secret_value()
        if not api_key:
            raise ProviderError("Language model unavailable")
        payload = {"model": self.settings.openai_model,
                   "messages": [{"role": "system", "content": SYSTEM_PROMPT}, *messages],
                   "stream": False, "max_completion_tokens": 256, "store": False}
        if self.settings.openai_reasoning_effort is not None:
            payload["reasoning_effort"] = self.settings.openai_reasoning_effort
        try:
            async with asyncio.timeout(self.settings.llm_timeout_seconds):
                response = await self.client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={"Authorization": "Bearer " + api_key},
                    json=payload,
                    timeout=self.settings.llm_timeout_seconds,
                )
                response.raise_for_status()
                choice = response.json()["choices"][0]
                message = choice["message"]
                if (choice["finish_reason"] != "stop" or not isinstance(message, dict)
                        or message.get("refusal") is not None):
                    raise ValueError("Incomplete or refused response")
                content = message["content"]
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("Empty content")
                return content.strip()
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, IndexError, TypeError):
            raise ProviderError("Language model unavailable") from None

    async def reply_with_tools(self, messages: list[dict[str, str]], execute_tool: ToolExecutor) -> str:
        """Prepare drafts only; confirmation and CRM writes are server operations."""
        api_key = self.settings.openai_api_key.get_secret_value()
        if not api_key:
            raise ProviderError("Language model unavailable")
        conversation: list[dict[str, Any]] = [
            {"role": "system", "content": CALLBACK_SYSTEM_PROMPT},
            *(dict(message) for message in messages),
        ]
        seen_call_ids: set[str] = set()
        try:
            async with asyncio.timeout(self.settings.llm_timeout_seconds):
                for _ in range(3):
                    payload = {
                        "model": self.settings.openai_model,
                        "messages": conversation,
                        "tools": CALLBACK_TOOLS,
                        "response_format": CALLBACK_RESPONSE_FORMAT,
                        "tool_choice": "auto",
                        "parallel_tool_calls": False,
                        "stream": False,
                        "max_completion_tokens": 512,
                        "store": False,
                    }
                    if self.settings.openai_reasoning_effort is not None:
                        payload["reasoning_effort"] = self.settings.openai_reasoning_effort
                    response = await self.client.post(
                        "https://api.openai.com/v1/chat/completions",
                        headers={"Authorization": "Bearer " + api_key},
                        json=payload,
                        timeout=self.settings.llm_timeout_seconds,
                    )
                    response.raise_for_status()
                    if len(response.content) > 512 * 1024:
                        raise ValueError("Oversized response")
                    choice = response.json()["choices"][0]
                    message = choice["message"]
                    if (not isinstance(message, dict) or message.get("role") != "assistant"
                            or message.get("refusal") is not None):
                        raise ValueError("Invalid or refused response")
                    calls = message.get("tool_calls")
                    if choice["finish_reason"] == "stop" and calls in (None, []):
                        return parse_callback_reply(message["content"])
                    if (choice["finish_reason"] != "tool_calls" or not isinstance(calls, list)
                            or len(calls) != 1):
                        raise ValueError("Incomplete or unexpected tool calls")
                    call = calls[0]
                    call_id = call["id"]
                    if (call.get("type") != "function" or not isinstance(call_id, str)
                            or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", call_id)
                            or call_id in seen_call_ids):
                        raise ValueError("Invalid tool call")
                    function = call["function"]
                    if function["name"] != "prepare_callback":
                        raise ValueError("Unknown tool")
                    arguments = parse_callback_arguments(function["arguments"])
                    seen_call_ids.add(call_id)
                    # The executor persists a draft or returns a fixed safe error.
                    # It never receives a confirmation or an assignee from the model.
                    try:
                        result = await execute_tool("prepare_callback", arguments)
                    except Exception:
                        raise ProviderError("Language model unavailable") from None
                    if not isinstance(result, dict):
                        raise ValueError("Invalid tool result")
                    if result.get("status") == "draft":
                        if not isinstance(result.get("draft_id"), str) or not result["draft_id"].strip():
                            raise ValueError("Missing persisted draft")
                        # Do not let another completion turn a draft into a claim
                        # that a CRM task or appointment was already created.
                        return CALLBACK_DRAFT_REPLY
                    if result.get("status") != "error":
                        raise ValueError("Invalid tool status")
                    conversation.extend([
                        {"role": "assistant", "content": None, "tool_calls": [call]},
                        {"role": "tool", "tool_call_id": call_id,
                         "content": json.dumps(result, ensure_ascii=False, allow_nan=False)},
                    ])
                raise ValueError("Tool round limit exceeded")
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise ProviderError("Language model unavailable") from None
