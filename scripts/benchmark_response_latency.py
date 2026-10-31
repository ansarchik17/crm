"""Manual live diagnostic: six model requests, no STT/TTS. May incur charges."""

import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import httpx

from voicecrm.config import Settings
from voicecrm.dialogue.prompt import SYSTEM_PROMPT


async def main():
    settings = Settings()
    if settings.llm_mode != "openai":
        raise SystemExit("Нужен LLM_MODE=openai")
    if not settings.openai_api_key.get_secret_value():
        raise SystemExit("Добавьте OPENAI_API_KEY в локальный .env")
    model = settings.openai_model
    endpoint = "https://api.openai.com/v1/chat/completions"
    api_key = settings.openai_api_key
    prompts = [
        "Можно узнать стоимость профессиональной чистки? Хочу прийти в среду вечером.",
        "Добрый день. Хочу консультацию по брекетам в субботу около одиннадцати.",
        "Мне нужна консультация стоматолога. Как можно записаться?",
    ]
    output = Path("recordings") / ("response_latency_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json")
    output.parent.mkdir(exist_ok=True)
    results = {"provider": settings.llm_mode, "model": model,
               "reasoning_effort": settings.openai_reasoning_effort,
               "deadline_seconds": settings.llm_timeout_seconds, "requests": []}
    async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
        for index, prompt in enumerate(prompts):
            for stream in ([False, True] if index % 2 == 0 else [True, False]):
                payload = {"model": model, "stream": stream, "max_completion_tokens": 256, "store": False,
                           "messages": [{"role": "system", "content": SYSTEM_PROMPT +
                                         "\nИдентификатор теста (не включай в ответ): " + uuid4().hex},
                                        {"role": "user", "content": prompt}]}
                if settings.openai_reasoning_effort is not None:
                    payload["reasoning_effort"] = settings.openai_reasoning_effort
                row = {"prompt": index + 1, "stream": stream}
                started = perf_counter()
                elapsed = lambda: round(perf_counter() - started, 3)
                try:
                    async with asyncio.timeout(settings.llm_timeout_seconds):
                        async with client.stream("POST", endpoint,
                                                 headers={"Authorization": "Bearer " + api_key.get_secret_value()},
                                                 json=payload) as response:
                            row.update(status=response.status_code, headers_seconds=elapsed())
                            response.raise_for_status()
                            if stream and "text/event-stream" in response.headers.get("content-type", ""):
                                row["sse"] = True
                                answer = ""
                                events = 0
                                async for line in response.aiter_lines():
                                    if not line.startswith("data:"):
                                        continue
                                    data = line[5:].strip()
                                    if data == "[DONE]":
                                        row["done"] = True
                                        break
                                    body = json.loads(data)
                                    for choice in body.get("choices", []):
                                        delta = choice.get("delta", {}).get("content")
                                        if isinstance(delta, str) and delta:
                                            answer += delta
                                            events += 1
                                            if answer.strip():
                                                row.setdefault("first_text_seconds", elapsed())
                                            if re.search(r"[.!?](?:\s|$)", answer):
                                                row.setdefault("first_sentence_seconds", elapsed())
                                        if choice.get("finish_reason"):
                                            row["finish_reason"] = choice["finish_reason"]
                                row.update(text=answer, text_events=events)
                            else:
                                await response.aread()
                                choice = response.json()["choices"][0]
                                row.update(sse=False, text=choice["message"].get("content"),
                                           finish_reason=choice.get("finish_reason"))
                except (httpx.HTTPError, TimeoutError, ValueError, KeyError, IndexError, TypeError) as error:
                    row["error"] = type(error).__name__
                row["total_seconds"] = elapsed()
                results["requests"].append(row)
                output.write_text(json.dumps(results, ensure_ascii=False, indent=2))
                print(json.dumps(row, ensure_ascii=False), flush=True)
    print("Saved:", output)


if __name__ == "__main__":
    asyncio.run(main())
