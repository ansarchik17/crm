import asyncio
from typing import Protocol

import httpx

from voicecrm.config import Settings
from voicecrm.dialogue.prompt import SYSTEM_PROMPT
from voicecrm.errors import ProviderError


class LanguageModel(Protocol):
    async def reply(self, messages: list[dict[str, str]]) -> str: ...


class DemoModel:
    async def reply(self, messages: list[dict[str, str]]) -> str:
        return "Это тестовый режим без AI. Сообщение принято. Подключите Qwen для проверки диалога."


class QwenModel:
    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client

    async def reply(self, messages: list[dict[str, str]]) -> str:
        payload = {"model": self.settings.qwen_model,
                   "messages": [{"role": "system", "content": SYSTEM_PROMPT}, *messages],
                   "stream": False, "max_tokens": 256}
        if self.settings.qwen_enable_thinking is not None:
            payload["chat_template_kwargs"] = {
                "enable_thinking": self.settings.qwen_enable_thinking}
        try:
            # Whole-request deadline also bounds servers that trickle response bytes.
            async with asyncio.timeout(self.settings.llm_timeout_seconds):
                response = await self.client.post(
                    self.settings.qwen_base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": "Bearer " + self.settings.qwen_api_key.get_secret_value()},
                    json=payload,
                    timeout=self.settings.llm_timeout_seconds,
                )
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                if not isinstance(content, str) or not content.strip():
                    raise ValueError("Empty content")
                return content.strip()
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, IndexError, TypeError):
            # Do not expose provider response bodies, request headers or credentials.
            raise ProviderError("Language model unavailable") from None
