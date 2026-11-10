"""Pool of OpenAI-compatible LLM endpoints with priority order and failover.

`ModelPool.stream()` yields chat-completion chunks from the first healthy model. A model that
errors, or doesn't produce its first token within `first_token_timeout`, is benched and the
next model is tried. Failover only happens before any output reached the caller; once a
model has started talking we can't take the words back, so a mid-stream failure is raised.
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

import openai
import yaml

log = logging.getLogger(__name__)

RATE_LIMIT_COOLDOWN_S = 60
OUTAGE_COOLDOWN_S = 30
HARD_FAILURE_COOLDOWN_S = 3600  # bad/expired key, no access to the model


class NoModelAvailable(Exception):
    pass


@dataclass
class PoolModel:
    provider: str
    model: str
    priority: int
    client: openai.AsyncOpenAI
    extra_body: dict = field(default_factory=dict)
    benched_until: float = 0.0

    @property
    def label(self) -> str:
        return f"{self.provider}/{self.model}"

    def healthy(self) -> bool:
        return time.monotonic() >= self.benched_until

    def bench(self, seconds: float, reason: str) -> None:
        self.benched_until = time.monotonic() + seconds
        log.warning("Benching %s for %ds: %s", self.label, seconds, reason)


def load_pool_models(path: str) -> list[PoolModel]:
    cfg = yaml.safe_load(Path(path).read_text())
    clients: dict[str, openai.AsyncOpenAI] = {}
    for name, p in cfg["providers"].items():
        key = os.environ.get(p["api_key_env"], "").strip()
        if not key:
            continue
        base_url = os.environ.get(p.get("base_url_env") or "", "").strip() or p["base_url"]
        # Retries are our job (fail over to the next model), not the SDK's.
        clients[name] = openai.AsyncOpenAI(api_key=key, base_url=base_url, max_retries=0, timeout=20)

    models = [
        PoolModel(m["provider"], m["model"], m["priority"], clients[m["provider"]], m.get("extra_body") or {})
        for m in cfg["models"]
        if m["provider"] in clients
    ]
    return sorted(models, key=lambda m: m.priority)


def _cooldown_for(exc: Exception) -> tuple[float, str]:
    if isinstance(exc, openai.RateLimitError):
        retry_after = exc.response.headers.get("retry-after") if exc.response is not None else None
        try:
            return min(float(retry_after), 600), "rate limited"
        except (TypeError, ValueError):
            return RATE_LIMIT_COOLDOWN_S, "rate limited"
    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError, openai.NotFoundError)):
        return HARD_FAILURE_COOLDOWN_S, f"{type(exc).__name__} (key expired or no access?)"
    if isinstance(exc, openai.APIStatusError) and exc.status_code == 402:
        return HARD_FAILURE_COOLDOWN_S, "payment required"
    if isinstance(exc, openai.BadRequestError):
        # Often a one-off malformed tool call (Groq: tool_use_failed). Skip for this turn only.
        return 0, f"bad request: {exc.message[:160]}"
    return OUTAGE_COOLDOWN_S, f"{type(exc).__name__}: {exc}"[:200]


class ModelPool:
    def __init__(self, models: list[PoolModel], first_token_timeout: float = 4.0, max_tokens: int = 1024):
        self.models = models
        self._first_token_timeout = first_token_timeout
        self._max_tokens = max_tokens

    @property
    def active_labels(self) -> list[str]:
        return [m.label for m in self.models]

    async def stream(self, messages: list[dict], tools: list[dict]) -> AsyncIterator[Any]:
        """Yield `(model_label, chunk)` pairs from the first model that answers."""
        candidates = [m for m in self.models if m.healthy()] or list(self.models)
        if not candidates:
            raise NoModelAvailable("No LLM provider configured; set GROQ_API_KEY (or another key) in .env")

        last_exc: Exception | None = None
        for model in candidates:
            started = time.monotonic()
            try:
                stream = await asyncio.wait_for(self._open(model, messages, tools), self._first_token_timeout)
                iterator = stream.__aiter__()
                remaining = max(0.5, self._first_token_timeout - (time.monotonic() - started))
                first = await asyncio.wait_for(iterator.__anext__(), remaining)
            except StopAsyncIteration:
                last_exc = RuntimeError(f"{model.label} returned an empty stream")
                model.bench(OUTAGE_COOLDOWN_S, "empty stream")
                continue
            except asyncio.TimeoutError:
                last_exc = TimeoutError(f"{model.label} too slow to first token")
                model.bench(OUTAGE_COOLDOWN_S, f"no first token in {self._first_token_timeout}s")
                continue
            except openai.APIError as exc:
                last_exc = exc
                seconds, reason = _cooldown_for(exc)
                if seconds:
                    model.bench(seconds, reason)
                else:
                    log.warning("%s failed this turn: %s", model.label, reason)
                continue

            log.info("LLM %s first token in %.0f ms", model.label, (time.monotonic() - started) * 1000)
            try:
                yield model.label, first
                async for chunk in iterator:
                    yield model.label, chunk
            finally:
                # Also runs when the consumer stops early (barge-in, grounding guard).
                close = getattr(stream, "close", None)
                if close is not None:
                    await close()
            return

        raise NoModelAvailable(f"All models failed; last error: {last_exc}")

    async def _open(self, model: PoolModel, messages: list[dict], tools: list[dict]):
        return await model.client.chat.completions.create(
            model=model.model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            max_tokens=self._max_tokens,
            stream=True,
            extra_body=model.extra_body or None,
        )
