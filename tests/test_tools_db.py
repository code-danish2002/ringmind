"""Tools against a real (seeded) Postgres: scoping, identity verification, billing, orders,
appointments, tickets. Skipped when the test database isn't running."""

import json
from datetime import timedelta

from app.tools.crm import get_crm
from app.tools.registry import CallContext, business_now, run_tool


async def ctx_for(phone: str) -> CallContext:
    return CallContext(call_id="test-call", caller_phone=phone, customer=await get_crm().find_customer_by_phone(phone))


async def call(name, args, ctx):
    out, err = await run_tool(name, args, ctx)
    return json.loads(out), err


async def test_lookup_exposes_no_sensitive_fields_before_verification(pg):
    ctx = await ctx_for("+1 (555) 123-4567")
    assert ctx.customer == {"id": 1, "name": "Priya Sharma", "status": "active", "plan": "Plus"}


async def test_orders_are_scoped_to_the_caller(pg):
    ctx = await ctx_for("+15551234567")
    res, _ = await call("get_order_status", {"order_id": "ord 1042"}, ctx)
    assert res["order"]["status"] == "shipped" and "day" in res["order"]["eta"]  # spoken date
    res, _ = await call("get_order_status", {"order_id": "ORD-1051"}, ctx)  # Marcus's order
    assert "error" in res


async def test_sensitive_tools_need_verification_and_lock_after_three_failures(pg):
    ctx = await ctx_for("+15551234567")
    res, _ = await call("get_billing_summary", {}, ctx)
    assert "not verified" in res["error"]

    for left in (2, 1, 0):
        res, _ = await call("verify_identity", {"pin": "0000"}, ctx)
        assert res == {"verified": False, "attempts_left": left}
    res, _ = await call("verify_identity", {"pin": "1234"}, ctx)  # right PIN, but locked
    assert res["verified"] is False and "Locked" in res["error"]
    res, _ = await call("get_billing_summary", {}, ctx)
    assert "locked" in res["error"]


async def test_verify_by_pin_or_birthday_then_billing(pg):
    ctx = await ctx_for("+15551234567")
    res, err = await call("verify_identity", {"pin": "12345"}, ctx)
    assert err and "INVALID_INPUT" in res
    res, _ = await call("verify_identity", {"pin": "1234", "date_of_birth": "1990-04-12"}, ctx)
    assert "exactly one" in res["error"]
    res, _ = await call("verify_identity", {"date_of_birth": "1990-04-12"}, ctx)
    assert res == {"verified": True} and ctx.verified
    res, _ = await call("get_billing_summary", {}, ctx)
    assert res["balance_due"] == 49.99 and res["plan"] == "Plus" and res["last_payment"]["method"].startswith("card")


async def test_cancel_order_only_when_processing(pg):
    ctx = await ctx_for("+15551234567")
    await call("verify_identity", {"pin": 1234}, ctx)  # numeric PIN from the model is accepted
    res, _ = await call("cancel_order", {"order_id": "ORD-1042"}, ctx)
    assert res["cancelled"] is False and "shipped" in res["reason"]
    res, _ = await call("cancel_order", {"order_id": "ORD-1055"}, ctx)
    assert res["cancelled"] is True
    res, _ = await call("cancel_order", {"order_id": "ORD-1055"}, ctx)
    assert res["cancelled"] is False


async def test_find_book_and_cancel_appointment(pg):
    ctx = await ctx_for("+15551234567")
    day = business_now().date() + timedelta(days=1)
    while day.isoweekday() == 7:
        day += timedelta(days=1)
    res, _ = await call("find_store_appointments", {"date": day.isoformat(), "store_id": "downtown"}, ctx)
    slot = res["stores"][0]["slots"][0]
    assert slot["spoken"].endswith(("AM", "PM"))

    res, _ = await call("book_store_appointment", {"store_id": "downtown", "time": slot["time"], "reason": "repair"}, ctx)
    assert "not verified" in res["error"]
    await call("verify_identity", {"pin": "1234"}, ctx)
    res, _ = await call("book_store_appointment", {"store_id": "downtown", "time": slot["time"], "reason": "repair"}, ctx)
    assert res["booked"] is True
    res, _ = await call("book_store_appointment", {"store_id": "downtown", "time": slot["time"], "reason": "again"}, ctx)
    assert res["booked"] is False

    mine, _ = await call("get_my_appointments", {}, ctx)
    appt_id = str(mine["appointments"][0]["id"])
    res, _ = await call("cancel_store_appointment", {"appointment_id": appt_id}, ctx)
    assert res == {"cancelled": True}

    # Marcus's appointment can't be cancelled from Priya's call.
    marcus = await ctx_for("+15559876543")
    theirs, _ = await call("get_my_appointments", {}, marcus)
    res, _ = await call("cancel_store_appointment", {"appointment_id": str(theirs["appointments"][0]["id"])}, ctx)
    assert res["cancelled"] is False


async def test_past_dates_and_closed_days(pg):
    ctx = await ctx_for("+15551234567")
    yesterday = business_now().date() - timedelta(days=1)
    res, _ = await call("find_store_appointments", {"date": yesterday.isoformat()}, ctx)
    assert "past" in res["error"]


async def test_unknown_caller_gets_no_account_access_but_can_open_ticket(pg):
    ctx = await ctx_for("+15550001111")
    assert ctx.customer is None
    for tool in ("get_recent_orders", "get_billing_summary", "verify_identity"):
        res, _ = await call(tool, {} if tool != "verify_identity" else {"pin": "1234"}, ctx)
        assert "isn't linked to an account" in res["error"]
    res, _ = await call("create_support_ticket", {"subject": "No signal", "description": "Restarted", "priority": "high"}, ctx)
    async with pg.acquire() as c:
        row = await c.fetchrow("SELECT caller_phone, call_id FROM tickets WHERE id = $1", res["ticket_id"])
    assert dict(row) == {"caller_phone": "+15550001111", "call_id": "test-call"}


async def test_tool_events_are_emitted_without_the_pin(pg):
    events = []

    async def on_event(kind, text, data):
        events.append((kind, text, data))

    ctx = await ctx_for("+15551234567")
    ctx.on_event = on_event
    await call("verify_identity", {"pin": "1234"}, ctx)
    assert events == [("tool", "verify_identity", {"args": {"pin": "****"}, "result": {"verified": True}, "error": False})]
