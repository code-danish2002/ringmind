"""Check every model in config/models.yaml whose key is set: does it answer, how fast does the
first token arrive, and does it call tools correctly?

    python -m scripts.verify_models

Keys are read from .env and never printed.
"""

import asyncio
import json
import time

import openai

from app.agent.llm import OPENAI_TOOLS
from app.agent.pool import PoolModel, load_pool_models
from app.config import get_settings

SPEECH_TEST = [
    {"role": "system", "content": "You are a phone assistant. Reply in one short spoken sentence."},
    {"role": "user", "content": "Hi, are you open on Saturdays?"},
]
TOOL_TEST = [
    {"role": "system", "content": "You are a phone assistant. Use tools to look things up."},
    {"role": "user", "content": "What's the status of my order ORD-1042?"},
]


async def first_token_ms(m: PoolModel, messages: list[dict], tools=None) -> tuple[float, str, list]:
    started = time.monotonic()
    stream = await m.client.chat.completions.create(
        model=m.model, messages=messages, tools=tools, max_tokens=300, stream=True, extra_body=m.extra_body or None
    )
    ttft, text, calls = None, "", {}
    async for chunk in stream:
        if not chunk.choices:
            continue
        d = chunk.choices[0].delta
        if (d.content or d.tool_calls) and ttft is None:
            ttft = (time.monotonic() - started) * 1000
        text += d.content or ""
        for tc in d.tool_calls or []:
            slot = calls.setdefault(tc.index, {"name": "", "args": ""})
            slot["name"] += (tc.function.name or "") if tc.function else ""
            slot["args"] += (tc.function.arguments or "") if tc.function else ""
    return ttft or (time.monotonic() - started) * 1000, text.strip(), list(calls.values())


async def check(m: PoolModel) -> str:
    try:
        ttft, text, _ = await first_token_ms(m, SPEECH_TEST)
        _, _, calls = await first_token_ms(m, TOOL_TEST, OPENAI_TOOLS)
    except openai.APIStatusError as e:
        return f"FAIL  {m.label:<32} HTTP {e.status_code}: {str(e.message)[:120]}"
    except Exception as e:
        return f"FAIL  {m.label:<32} {type(e).__name__}: {str(e)[:120]}"

    tool_ok = False
    for c in calls:
        try:
            tool_ok = tool_ok or (c["name"] == "get_order_status" and "1042" in json.loads(c["args"] or "{}").get("order_id", ""))
        except json.JSONDecodeError:
            pass
    leak = " <think> LEAK" if "<think>" in text.lower() else ""
    speed = "ok" if ttft < get_settings().llm_first_token_timeout * 1000 else "SLOW"
    return (
        f"{'OK  ' if tool_ok and speed == 'ok' else 'WARN'}  {m.label:<32} first token {ttft:5.0f} ms ({speed}), "
        f"tools {'ok' if tool_ok else 'NOT OK: ' + json.dumps(calls)[:80]}{leak}\n      says: {text[:100]!r}"
    )


async def main() -> None:
    models = load_pool_models(get_settings().llm_models_file)
    if not models:
        print("No provider keys set in .env (GROQ_API_KEY, GLM_API_KEY, ...).")
        return
    print(f"Checking {len(models)} model(s) in priority order...\n")
    for line in await asyncio.gather(*(check(m) for m in models)):
        print(line)


if __name__ == "__main__":
    asyncio.run(main())
