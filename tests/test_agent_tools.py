import json

import pytest

from app.agent.llm import SentenceSplitter, VoiceAgent
from app.config import Settings
from app.tools.registry import CallContext, run_tool, validate_input


def test_sentence_splitter_streams_sentences():
    sp = SentenceSplitter()
    out = []
    for tok in ["Sure", ", your order", " shipped. It", " arrives Friday!", " Anything else"]:
        out += sp.feed(tok)
    assert out == ["Sure, your order shipped.", "It arrives Friday!"]
    assert sp.flush() == "Anything else"


def test_sentence_splitter_soft_flush_on_long_clause():
    sp = SentenceSplitter(soft_limit=40)
    out = sp.feed("We have three plans available for you today, each with different data options")
    assert out and out[0].endswith("today,")


def test_validate_input():
    assert validate_input("get_order_status", {"order_id": "ORD-1"}) is None
    assert "missing" in validate_input("get_order_status", {})
    assert "unexpected" in validate_input("get_recent_orders", {"x": "1"})
    assert "one of" in validate_input("create_support_ticket", {"subject": "a", "description": "b", "priority": "meh"})
    assert "unknown" in validate_input("rm_rf", {})


def test_history_repair_and_turn_merge():
    agent = VoiceAgent(Settings(), CallContext(call_id="t", caller_phone="+1"), pool=object())
    agent._add_user_text("hello")
    agent.messages.append({"role": "assistant", "content": None, "tool_calls": [{"id": "tc_1", "type": "function", "function": {"name": "get_recent_orders", "arguments": "{}"}}]})
    agent._repair_dangling_tool_calls()  # interrupted while the tool was running
    assert agent.messages[-1] == {"role": "tool", "tool_call_id": "tc_1", "content": "Interrupted by the caller before completion."}
    agent.note("The caller interrupted you.")
    agent._add_user_text("actually never mind")
    assert agent.messages[-1]["role"] == "user"
    assert agent.messages[-1]["content"].startswith("[The caller interrupted you.]")

    # Interrupted before any answer: the next utterance merges into the same user turn.
    agent._add_user_text("and one more thing")
    assert agent.messages[-1]["content"].endswith("actually never mind\nand one more thing")


def test_splitter_strips_think_blocks_and_markdown():
    sp = SentenceSplitter()
    out = sp.feed("<think>user wants plan")
    assert out == []
    out += sp.feed(" price</think>The **Unlimited** plan is $69.99. ")
    out += sp.feed("Want details? 😊")
    assert out == ["The Unlimited plan is $69.99.", "Want details?"]
    assert sp.flush() == ""


def test_spoken_end_call_is_stripped_and_flagged():
    sp = SentenceSplitter()
    out = sp.feed("You're welcome, Priya. Have a great day! end call")
    out.append(sp.flush())
    assert out == ["You're welcome, Priya.", "Have a great day!", ""]
    assert sp.saw_end_call


def test_stray_think_tag_is_not_spoken():
    from app.agent.llm import speakable

    assert speakable("Sure. </think Yes, you can.") == "Sure. Yes, you can."


def test_order_dates_are_spelled_out_for_the_model():
    from app.tools.registry import spoken_dates

    o = spoken_dates({"id": "ORD-1042", "eta": "2026-11-01", "created_at": "2026-10-27"})
    assert o["eta"] == "Sunday 1 November 2026" and o["created_at"] == "Tuesday 27 October 2026"


def test_tool_call_written_as_text_is_not_spoken():
    sp = SentenceSplitter()
    out = sp.feed('Goodbye! { "reason": "caller ended call" }')
    out.append(sp.flush())
    assert [s for s in out if s] == ["Goodbye!"] and sp.saw_end_call
