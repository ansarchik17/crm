"""Explicit live API benchmark; run manually (requests may be billed)."""
import asyncio
import json
from pathlib import Path
from time import perf_counter
from statistics import median
from uuid import uuid4
import httpx
from voicecrm.config import Settings
from voicecrm.dialogue.prompt import SYSTEM_PROMPT

async def main():
    s = Settings()
    Path("recordings").mkdir(exist_ok=True)
    prompts = [
        'Здравствуйте, меня зовут Андрей. Я хочу уточнить информацию о вашей услуге, сколько она стоит и когда можно записаться. Мне удобно завтра после трех часов дня.',
        'Хочу узнать цену и записаться на завтра. Какие данные нужны?',
        'Можно перенести мою запись на пятницу после обеда?',
    ]
    variants = [('baseline', {}), ('prompt_no_thinking', {}),
                ('template_no_thinking', {'chat_template_kwargs': {'enable_thinking': False}})]
    results = []
    async with httpx.AsyncClient(timeout=60) as client:
        for i, prompt in enumerate(prompts):
            for name, extra in variants[i:] + variants[:i]:
                started = perf_counter()
                row = {'prompt': i + 1, 'variant': name}
                try:
                    response = await client.post(s.qwen_base_url.rstrip('/') + '/chat/completions',
                        headers={'Authorization': 'Bearer ' + s.qwen_api_key.get_secret_value()},
                        json={'model': s.qwen_model, 'messages': [{'role': 'system', 'content': SYSTEM_PROMPT + '\nИдентификатор теста (не включай в ответ): ' + uuid4().hex + ('\n/no_think' if name == 'prompt_no_thinking' else '')},
                              {'role': 'user', 'content': prompt}], 'stream': False, 'max_tokens': 256, **extra})
                    row['status'] = response.status_code
                    if response.is_success:
                        body = response.json()
                        choice = body['choices'][0]
                        row.update(text=choice['message'].get('content'), finish_reason=choice.get('finish_reason'),
                                   usage=body.get('usage'), reasoning_chars=len(choice['message'].get('reasoning_content') or ''))
                except (httpx.HTTPError, ValueError, KeyError, IndexError) as error:
                    row['error'] = type(error).__name__
                row['seconds'] = round(perf_counter() - started, 3)
                results.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)
                Path('recordings/qwen_benchmark_uncached.json').write_text(json.dumps(results, ensure_ascii=False, indent=2))
    for name, _ in variants:
        rows = [r for r in results if r['variant'] == name and r.get('status') == 200 and r.get('text') and r.get('finish_reason') == 'stop']
        print(name, 'successful:', len(rows), 'median:', median(r['seconds'] for r in rows) if rows else None)

if __name__ == '__main__':
    asyncio.run(main())
