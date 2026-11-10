"""Tools the voice agent can call: account and order lookups, identity checks, billing, order
cancellation, store appointments, knowledge base search, tickets, callbacks, call control.

Security model:
- Every tool is scoped to the *caller* (identified by caller ID). The model never passes a
  customer id, so a caller can't talk the bot into reading someone else's account.
- Caller ID can be spoofed, so anything sensitive (billing, cancelling, booking) also needs
  `verify_identity` (4-digit PIN or date of birth) to have succeeded on this call. Three wrong
  attempts lock verification for the rest of the call.
"""

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from ..config import get_settings
from .crm import SlotUnavailable, get_crm
from .knowledge import search_knowledge_base

log = logging.getLogger(__name__)

MAX_VERIFY_ATTEMPTS = 3


@dataclass
class CallContext:
    call_id: str
    caller_phone: str
    customer: dict | None = None
    recent_orders: list[dict] = field(default_factory=list)  # preloaded so simple questions skip a tool call
    verified: bool = False
    verify_attempts: int = 0
    # Wired up by the transport; None when unsupported.
    transfer: Callable[[str], Awaitable[bool]] | None = None
    hangup: Callable[[], Awaitable[None]] | None = None
    # Call recorder hook: (kind, text, data).
    on_event: Callable[[str, str, dict], Awaitable[None]] | None = None
    pending_actions: list[str] = field(default_factory=list)  # run after the bot finishes speaking
    said_goodbye: bool = False  # bot said goodbye without calling end_call
    models_used: set[str] = field(default_factory=set)

    async def emit(self, kind: str, text: str, data: dict | None = None) -> None:
        if self.on_event is not None:
            await self.on_event(kind, text, data or {})


def business_now() -> datetime:
    return datetime.now(ZoneInfo(get_settings().timezone))


# -- speech-friendly values --------------------------------------------------------------------------

_ISO_DATE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_ISO_DATETIME = re.compile(r"^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2})$")


def spoken_time(dt: datetime) -> str:
    hour = dt.hour % 12 or 12
    minutes = f":{dt.minute:02d}" if dt.minute else ""
    return f"{hour}{minutes} {'AM' if dt.hour < 12 else 'PM'}"


def spoken_dates(record: dict) -> dict:
    """'2026-11-01' -> 'Sunday 1 November 2026'; '2026-11-10 14:30' -> '... at 2:30 PM'.

    Models read ISO dates badly and get weekdays wrong, so tool results carry them spelled out.
    """
    out = dict(record)
    for key, value in record.items():
        if not isinstance(value, str):
            continue
        if m := _ISO_DATE.match(value):
            d = date(*map(int, m.groups()))
            out[key] = f"{d:%A} {d.day} {d:%B %Y}"
        elif m := _ISO_DATETIME.match(value):
            dt = datetime(*map(int, m.groups()))
            out[key] = f"{dt:%A} {dt.day} {dt:%B %Y} at {spoken_time(dt)}"
    return out


# -- tool definitions ------------------------------------------------------------------------------


def _schema(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}


_DATE = {"type": "string", "description": "YYYY-MM-DD", "pattern": r"^\d{4}-\d{2}-\d{2}$"}

TOOLS: list[dict] = [
    {
        "name": "verify_identity",
        "description": "Verify the caller before billing details, cancellations or appointment changes. Ask for their 4-digit account PIN, or their date of birth if they don't know it, then pass exactly one of them.",
        "input_schema": _schema(
            {
                "pin": {"type": "string", "description": "4-digit PIN", "pattern": r"^\d{4}$"},
                "date_of_birth": _DATE,
            },
            [],
        ),
    },
    {
        "name": "get_recent_orders",
        "description": "List the caller's recent orders with status, ship date, ETA, carrier and tracking. Use when they ask about an order without a number.",
        "input_schema": _schema({}, []),
    },
    {
        "name": "get_order_status",
        "description": "Look up one of the caller's orders by order number (e.g. 'ORD-1042').",
        "input_schema": _schema({"order_id": {"type": "string", "description": "Like ORD-1042"}}, ["order_id"]),
    },
    {
        "name": "cancel_order",
        "description": "Cancel one of the caller's orders that hasn't shipped yet. Requires verify_identity. Confirm the order with the caller first.",
        "input_schema": _schema({"order_id": {"type": "string"}}, ["order_id"]),
    },
    {
        "name": "get_billing_summary",
        "description": "Balance due, next bill date, plan price, AutoPay and last payment. Requires verify_identity.",
        "input_schema": _schema({}, []),
    },
    {
        "name": "find_store_appointments",
        "description": "Free in-store appointment times on a date (store visits: device setup, repairs, trade-ins). Optionally for one store.",
        "input_schema": _schema(
            {"date": _DATE, "store_id": {"type": "string", "description": "Optional store id from a previous result"}},
            ["date"],
        ),
    },
    {
        "name": "book_store_appointment",
        "description": "Book an in-store appointment at a time returned by find_store_appointments. Requires verify_identity. Confirm store, day and time with the caller first.",
        "input_schema": _schema(
            {
                "store_id": {"type": "string"},
                "time": {"type": "string", "description": "Exact 'time' value from find_store_appointments, YYYY-MM-DD HH:MM", "pattern": r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$"},
                "reason": {"type": "string"},
            },
            ["store_id", "time", "reason"],
        ),
    },
    {
        "name": "get_my_appointments",
        "description": "The caller's upcoming store appointments.",
        "input_schema": _schema({}, []),
    },
    {
        "name": "cancel_store_appointment",
        "description": "Cancel one of the caller's upcoming appointments (id from get_my_appointments). Requires verify_identity.",
        "input_schema": _schema({"appointment_id": {"type": "string"}}, ["appointment_id"]),
    },
    {
        "name": "search_knowledge_base",
        "description": "Search company documentation (plans, pricing, policies, returns, store hours, troubleshooting). Use for any factual question about the company instead of guessing.",
        "input_schema": _schema({"query": {"type": "string", "description": "A focused search query"}}, ["query"]),
    },
    {
        "name": "create_support_ticket",
        "description": "Open a support ticket for an issue that can't be solved on this call. Confirm the summary with the caller first.",
        "input_schema": _schema(
            {
                "subject": {"type": "string"},
                "description": {"type": "string", "description": "What happened, what was tried, what the caller wants"},
                "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"]},
            },
            ["subject", "description", "priority"],
        ),
    },
    {
        "name": "schedule_callback",
        "description": "Schedule a call back from a human agent to the caller's number.",
        "input_schema": _schema(
            {
                "preferred_time": {"type": "string", "description": "When the caller wants to be called, as they said it"},
                "reason": {"type": "string"},
            },
            ["preferred_time", "reason"],
        ),
    },
    {
        "name": "transfer_to_human",
        "description": "Transfer the call to a live agent. Use when the caller asks for a person, is upset, or the request is outside what you can do.",
        "input_schema": _schema({"reason": {"type": "string"}}, ["reason"]),
    },
    {
        "name": "end_call",
        "description": "Hang up once the conversation is finished. Say your goodbye in the same reply.",
        "input_schema": _schema({"reason": {"type": "string"}}, ["reason"]),
    },
]

_TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}
NEEDS_VERIFICATION = {"cancel_order", "get_billing_summary", "book_store_appointment", "cancel_store_appointment"}
NEEDS_CUSTOMER = NEEDS_VERIFICATION | {"verify_identity", "get_recent_orders", "get_order_status", "get_my_appointments"}


def validate_input(name: str, args: Any) -> str | None:
    """Return an error string if `args` doesn't match the tool's schema."""
    tool = _TOOLS_BY_NAME.get(name)
    if tool is None:
        return f"unknown tool {name!r}"
    if not isinstance(args, dict):
        return "input must be an object"
    schema = tool["input_schema"]
    props = schema["properties"]
    for key in schema["required"]:
        if key not in args:
            return f"missing required field {key!r}"
    for key, value in args.items():
        if key not in props:
            return f"unexpected field {key!r}"
        if isinstance(value, int) and not isinstance(value, bool):
            value = args[key] = str(value)  # models often send ids/PINs as numbers
        if not isinstance(value, str) or not value.strip():
            return f"field {key!r} must be a non-empty string"
        if "enum" in props[key] and value not in props[key]["enum"]:
            return f"field {key!r} must be one of {props[key]['enum']}"
        if "pattern" in props[key] and not re.match(props[key]["pattern"], value.strip()):
            return f"field {key!r} must look like {props[key].get('description', props[key]['pattern'])}"
    return None


async def run_tool(name: str, args: dict, ctx: CallContext) -> tuple[str, bool]:
    """Execute a tool. Returns (result_json, is_error)."""
    error = validate_input(name, args)
    if error:
        result, is_error = {"INVALID_INPUT": error}, True
    else:
        try:
            result, is_error = await _dispatch(name, args, ctx), False
        except Exception as exc:
            log.exception("[%s] tool %s failed", ctx.call_id, name)
            result, is_error = {"error": "The system had a problem. Apologise and offer a ticket or callback."}, True
            await ctx.emit("system", f"tool {name} crashed: {type(exc).__name__}", {})
    # The PIN never reaches the call log.
    logged_args = {k: ("****" if k == "pin" else v) for k, v in args.items()} if isinstance(args, dict) else args
    await ctx.emit("tool", name, {"args": logged_args, "result": result, "error": is_error})
    return json.dumps(result, default=str), is_error


async def _dispatch(name: str, args: dict, ctx: CallContext) -> dict:
    crm = get_crm()
    customer_id = ctx.customer["id"] if ctx.customer else None

    if name in NEEDS_CUSTOMER and customer_id is None:
        return {"error": "This phone number isn't linked to an account, so account details can't be accessed. Offer a ticket, callback or transfer."}
    if name in NEEDS_VERIFICATION and not ctx.verified:
        if ctx.verify_attempts >= MAX_VERIFY_ATTEMPTS:
            return {"error": "Verification is locked after too many attempts. Offer a callback or transfer."}
        return {"error": "Caller not verified yet. Ask for their 4-digit PIN or date of birth and call verify_identity first."}

    if name == "verify_identity":
        if ctx.verified:
            return {"verified": True}
        if ctx.verify_attempts >= MAX_VERIFY_ATTEMPTS:
            return {"verified": False, "error": "Locked after too many attempts. Offer a callback or transfer."}
        pin, dob = args.get("pin"), args.get("date_of_birth")
        if bool(pin) == bool(dob):
            return {"error": "Pass exactly one of pin or date_of_birth."}
        ctx.verify_attempts += 1
        try:
            ok = await crm.verify_customer(customer_id, pin=pin, date_of_birth=date.fromisoformat(dob) if dob else None)
        except ValueError:
            ok = False
        ctx.verified = ok
        left = MAX_VERIFY_ATTEMPTS - ctx.verify_attempts
        return {"verified": True} if ok else {"verified": False, "attempts_left": left}

    if name == "get_recent_orders":
        return {"orders": [spoken_dates(o) for o in await crm.recent_orders(customer_id)]}

    if name == "get_order_status":
        order = await crm.get_order(args["order_id"].strip(), customer_id)
        return {"order": spoken_dates(order)} if order else {"error": "No order with that number on this caller's account."}

    if name == "cancel_order":
        return await crm.cancel_order(args["order_id"].strip(), customer_id)

    if name == "get_billing_summary":
        summary = await crm.billing_summary(customer_id)
        if summary.get("last_payment"):
            summary["last_payment"] = spoken_dates(summary["last_payment"])
        return spoken_dates(summary)

    if name == "find_store_appointments":
        try:
            day = date.fromisoformat(args["date"])
        except ValueError:
            return {"error": "Invalid date."}
        now = business_now()
        if day < now.date():
            return {"error": "That date is in the past."}
        stores = await crm.available_slots(day, now, args.get("store_id"))
        for s in stores:
            s["slots"] = [{"time": t, "spoken": spoken_dates({"t": t})["t"]} for t in s["slots"]]
        return {"date": spoken_dates({"d": args["date"]})["d"], "stores": stores} if stores else {
            "stores": [], "note": "No free times that day (stores may be closed). Suggest another day."
        }

    if name == "book_store_appointment":
        try:
            starts_at = datetime.strptime(args["time"].strip(), "%Y-%m-%d %H:%M")
        except ValueError:
            return {"error": "Invalid time."}
        try:
            booked = await crm.book_appointment(customer_id, args["store_id"].strip(), starts_at, args["reason"], business_now())
        except SlotUnavailable as e:
            return {"booked": False, "error": str(e) + " Offer other times from find_store_appointments."}
        return {"booked": True, "appointment": spoken_dates(booked)}

    if name == "get_my_appointments":
        return {"appointments": [spoken_dates(a) for a in await crm.upcoming_appointments(customer_id, business_now())]}

    if name == "cancel_store_appointment":
        try:
            appointment_id = int(args["appointment_id"])
        except ValueError:
            return {"error": "Invalid appointment id."}
        ok = await crm.cancel_appointment(appointment_id, customer_id)
        return {"cancelled": ok} if ok else {"cancelled": False, "error": "No upcoming appointment with that id on this account."}

    if name == "search_knowledge_base":
        hits = await search_knowledge_base(args["query"])
        return {"results": hits} if hits else {"results": [], "note": "Nothing relevant found. Don't guess; offer a ticket or transfer."}

    if name == "create_support_ticket":
        ticket_id = await crm.create_ticket(
            customer_id, ctx.caller_phone, ctx.call_id, args["subject"], args["description"], args["priority"]
        )
        return {"ticket_id": ticket_id, "status": "open"}

    if name == "schedule_callback":
        cb_id = await crm.schedule_callback(customer_id, ctx.caller_phone, ctx.call_id, args["preferred_time"], args["reason"])
        return {"callback_id": cb_id, "phone": ctx.caller_phone, "preferred_time": args["preferred_time"]}

    if name == "transfer_to_human":
        if ctx.transfer is None:
            return {"error": "Live transfer isn't available right now. Offer a callback or ticket instead."}
        ctx.pending_actions.append(f"transfer:{args['reason']}")
        return {"status": "transfer will start after you finish speaking"}

    if name == "end_call":
        ctx.pending_actions.append("hangup")
        return {"status": "call will end after you finish speaking"}

    raise ValueError(f"unhandled tool {name}")
