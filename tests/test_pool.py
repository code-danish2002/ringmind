"""LLM pool failover and the agent's streaming tool loop, with fake OpenAI-compatible clients."""

import asyncio
import json
import time
from types import SimpleNamespace as NS

import httpx
import openai
import pytest

from app.agent.llm import VoiceAgent
from app.agent.pool import ModelPool, NoModelAvailable, PoolModel, load_pool_models
from app.config import Settings
from app.tools.registry import CallContext


def chunk(content=None, finish=None, tool_calls=None):
    return NS(choices=[NS(delta=NS(content=content, tool_calls=tool_calls), finish_reason=finish)])


def tool_delta(index, id=None, name=None, args=None):
    return NS(index=index, id=id, function=NS(name=name, arguments=args))


class Stream:
    def __init__(self, chunks, delay=0.0):
        self._chunks, self._delay = iter(chunks), delay

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(self._delay)
        try:
            return next(self._chunks)
        except StopIteration:
            raise StopAsyncIteration


def fake_client(*turns, delay=0.0):
    """Each create() call consumes the next turn: an exception or a list of chunks."""
    turns = list(turns)
    calls = []

    async def create(**kw):
        calls.append({**kw, "messages": list(kw.get("messages", []))})
        turn = turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        return Stream(turn, delay)

    return NS(chat=NS(completions=NS(create=create)), calls=calls)


def api_error(cls, status, headers=None):
    resp = httpx.Response(status, request=httpx.Request("POST", "http://x"), headers=headers or {})
    return cls("boom", response=resp, body=None)


async def collect(pool):
    return [e async for e in pool.stream([], [])]


async def test_fails_over_on_rate_limit_and_honours_retry_after():
    a = PoolModel("groq", "a", 10, fake_client(api_error(openai.RateLimitError, 429, {"retry-after": "7"})))
    b = PoolModel("glm", "b", 20, fake_client([chunk("Hi"), chunk(finish="stop")]))
    events = await collect(ModelPool([a, b]))
    assert {label for label, _ in events} == {"glm/b"}
    assert 6 < a.benched_until - time.monotonic() <= 7
    assert not a.healthy() and b.healthy()


async def test_expired_key_is_benched_for_an_hour():
    a = PoolModel("glm", "a", 10, fake_client(api_error(openai.AuthenticationError, 401)))
    b = PoolModel("groq", "b", 20, fake_client([chunk("ok")]))
    await collect(ModelPool([a, b]))
    assert a.benched_until - time.monotonic() > 3000


async def test_fails_over_when_first_token_is_too_slow():
    slow = PoolModel("groq", "slow", 10, fake_client([chunk("late")], delay=1))
    fast = PoolModel("groq", "fast", 20, fake_client([chunk("ok")]))
    events = await collect(ModelPool([slow, fast], first_token_timeout=0.1))
    assert events[0][0] == "groq/fast"


async def test_all_failing_raises():
    a = PoolModel("groq", "a", 10, fake_client(api_error(openai.InternalServerError, 500)))
    with pytest.raises(NoModelAvailable):
        await collect(ModelPool([a]))
    with pytest.raises(NoModelAvailable):
        await collect(ModelPool([]))


def test_yaml_only_activates_providers_with_keys(monkeypatch):
    for k in ("GROQ_API_KEY", "GLM_API_KEY", "CEREBRAS_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "test")
    models = load_pool_models("config/models.yaml")
    assert models and {m.provider for m in models} == {"groq"}
    assert [m.priority for m in models] == sorted(m.priority for m in models)
    monkeypatch.setenv("GLM_API_KEY", "test")
    assert "glm/glm-5.3" in [m.label for m in load_pool_models("config/models.yaml")]


async def test_agent_streams_tool_call_then_answer(pg):
    client = fake_client(
        # Turn 1: tool call with arguments split across chunks (as providers stream them).
        [
            chunk(tool_calls=[tool_delta(0, id="tc1", name="create_support_ticket", args='{"subject": "No sig')]),
            chunk(tool_calls=[tool_delta(0, args='nal", "description": "Restarted", "priority": "high"}')]),
            chunk(finish="tool_calls"),
        ],
        # Turn 2: spoken answer, with a think block and a spoken "end call" to clean up.
        [chunk("<think>ticket made</think>Done, ticket one is open. "), chunk("Goodbye! end_call"), chunk(finish="stop")],
    )
    ctx = CallContext(call_id="t", caller_phone="+15550001111")
    agent = VoiceAgent(Settings(), ctx, pool=ModelPool([PoolModel("groq", "m", 10, client)]))
    said = [s async for s in agent.respond("my phone has no signal")]

    assert said == ["One moment.", "Done, ticket one is open.", "Goodbye!"]
    assert ctx.pending_actions == ["hangup"]
    tool_msg = next(m for m in agent.messages if m["role"] == "tool")
    assert json.loads(tool_msg["content"])["ticket_id"] == 1
    # Second request carried the assistant tool call and its result.
    roles = [m["role"] for m in client.calls[1]["messages"]]
    assert roles == ["system", "user", "assistant", "tool"]


async def test_goodbye_with_end_call_does_not_ask_the_model_again():
    client = fake_client(
        [
            chunk("Glad I could help. Goodbye! "),
            chunk(tool_calls=[tool_delta(0, id="tc1", name="end_call", args='{"reason": "done"}')]),
            chunk(finish="tool_calls"),
        ],
    )
    ctx = CallContext(call_id="t", caller_phone="+15550001111")
    agent = VoiceAgent(Settings(), ctx, pool=ModelPool([PoolModel("groq", "m", 10, client)]))
    said = [s async for s in agent.respond("that's all, bye")]
    assert said == ["Glad I could help.", "Goodbye!"]
    assert len(client.calls) == 1 and ctx.pending_actions == ["hangup"]


def test_unicode_spacing_is_made_plain():
    from app.agent.llm import speakable

    assert speakable("ORD‑1042 arrives at 9 am") == "ORD-1042 arrives at 9 am"


async def test_goodbye_without_end_call_is_flagged():
    client = fake_client([chunk("You're welcome, Priya. Have a great day!"), chunk(finish="stop")], [chunk("Sure, it shipped.")])
    ctx = CallContext(call_id="t", caller_phone="+1")
    agent = VoiceAgent(Settings(), ctx, pool=ModelPool([PoolModel("groq", "m", 10, client)]))
    [s async for s in agent.respond("thanks, bye")]
    assert ctx.said_goodbye and ctx.pending_actions == []
    [s async for s in agent.respond("oh wait, where's my order?")]
    assert not ctx.said_goodbye


def test_grounding_detects_invented_amounts_and_dates():
    from app.agent.grounding import Grounding

    g = Grounding()
    g.add('{"balance_due": 0.0, "next_bill_date": "Friday 13 November 2026", "monthly_price": 69.99}')
    g.add("Caller: is it still $69.99?")
    assert g.unsupported("Your balance is $0.00 and your plan is $69.99 a month.") is None
    assert g.unsupported("Your next bill is on November 13th.") is None
    assert g.unsupported("It's due on the 13th of November.") is None
    assert g.unsupported("Your balance is $42.75.") == "the amount $42.75"
    assert g.unsupported("Your next bill is on December 5, 2026.") == "the date December 5"
    assert g.unsupported("Order ORD-1042 has 2 items and arrives Tuesday.") is None  # plain numbers are fine


async def test_agent_blocks_invented_balance_and_retries_with_the_tool(pg):
    from app.tools.crm import get_crm

    client = fake_client(
        # Turn 1: the model invents a balance instead of calling the billing tool.
        [chunk("You're verified. "), chunk("Your balance is $42.75. "), chunk("Anything else?"), chunk(finish="stop")],
        # Turn 2 (after the system check): it calls the tool...
        [chunk(tool_calls=[tool_delta(0, id="b1", name="get_billing_summary", args="{}")]), chunk(finish="tool_calls")],
        # ...and answers from the result.
        [chunk("Your balance is $0.00, and your next bill is due soon."), chunk(finish="stop")],
    )
    ctx = CallContext(call_id="g1", caller_phone="+15559876543")
    ctx.customer = await get_crm().find_customer_by_phone("+15559876543")
    ctx.verified = True
    agent = VoiceAgent(Settings(), ctx, pool=ModelPool([PoolModel("groq", "m", 10, client)]))
    said = [s async for s in agent.respond("what's my balance?")]

    assert "Your balance is $42.75." not in said
    assert said[0] == "You're verified." and said[-1].startswith("Your balance is $0.00")
    assert any("System check" in m["content"] for m in client.calls[1]["messages"] if m["role"] == "user")


async def test_agent_gives_up_safely_after_two_invented_values():
    client = fake_client(
        [chunk("Your balance is $42.75."), chunk(finish="stop")],
        [chunk("Sorry, it's $41.10."), chunk(finish="stop")],
    )
    ctx = CallContext(call_id="g2", caller_phone="+1")
    agent = VoiceAgent(Settings(), ctx, pool=ModelPool([PoolModel("groq", "m", 10, client)]))
    said = [s async for s in agent.respond("what's my balance?")]
    assert said == ["I'm sorry, I can't confirm that detail right now. I can have someone call you back with it, if you'd like."]


async def test_end_call_refused_when_caller_is_not_done():
    client = fake_client(
        [chunk("Booked. Anything else I can help with? "),
         chunk(tool_calls=[tool_delta(0, id="e1", name="end_call", args='{"reason": "done"}')]), chunk(finish="tool_calls")],
        [chunk("Happy to help further."), chunk(finish="stop")],
    )
    ctx = CallContext(call_id="e", caller_phone="+1")
    agent = VoiceAgent(Settings(), ctx, pool=ModelPool([PoolModel("groq", "m", 10, client)]))
    said = [s async for s in agent.respond("yes please book it")]
    assert ctx.pending_actions == [] and said[-1] == "Happy to help further."
    tool_msg = next(m for m in agent.messages if m["role"] == "tool")
    assert "Not ended" in tool_msg["content"]


async def test_end_call_allowed_after_caller_says_bye():
    client = fake_client(
        [chunk("You're welcome, have a great day! "),
         chunk(tool_calls=[tool_delta(0, id="e2", name="end_call", args='{"reason": "done"}')]), chunk(finish="tool_calls")],
    )
    ctx = CallContext(call_id="e", caller_phone="+1")
    agent = VoiceAgent(Settings(), ctx, pool=ModelPool([PoolModel("groq", "m", 10, client)]))
    [s async for s in agent.respond("that's all, thanks, bye")]
    assert ctx.pending_actions == ["hangup"]


def test_plain_bye_counts_as_goodbye():
    from app.agent.llm import _GOODBYE

    for text in ("Bye.", "You're welcome! Bye.", "Take care!", "Goodbye!", "Have a great day."):
        assert _GOODBYE.search(text), text
    assert not _GOODBYE.search("Your order is on its way.")
