"""Company systems the voice agent can read and change: customers, billing, orders, store
appointments, tickets and callbacks.

`PostgresCRM` runs against the demo schema in db/schema.sql. To connect your real systems
(Salesforce, HubSpot, Shopify, Zendesk, your own database or API), implement the same methods
in another class and return it from `get_crm()`. Every method takes the *caller's* customer id;
the agent never chooses whose data to read.

All values returned are JSON-friendly: dates as 'YYYY-MM-DD', times as 'YYYY-MM-DD HH:MM',
money as float.
"""

import base64
import hashlib
import hmac
import os
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import asyncpg

from .. import db

PIN_ITERATIONS = 200_000
SLOT_MINUTES = 30


class SlotUnavailable(Exception):
    pass


# -- helpers -------------------------------------------------------------------------------------


def normalize_phone(phone: str) -> str:
    digits = "".join(c for c in phone if c.isdigit())
    return f"+{digits}" if digits else ""


def hash_pin(pin: str, *, salt: bytes | None = None, iterations: int = PIN_ITERATIONS) -> str:
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, iterations)
    return f"pbkdf2-sha256${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def check_pin(pin: str, stored: str | None) -> bool:
    if not stored:
        return False
    try:
        _, iterations, salt_b64, hash_b64 = stored.split("$")
        digest = hashlib.pbkdf2_hmac("sha256", pin.encode(), base64.b64decode(salt_b64), int(iterations))
        return hmac.compare_digest(digest, base64.b64decode(hash_b64))
    except (ValueError, TypeError):
        return False


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, time):
        return value.strftime("%H:%M")
    return value


def _row(record: asyncpg.Record | None) -> dict | None:
    return {k: _plain(v) for k, v in record.items()} if record is not None else None


def _rows(records: list[asyncpg.Record]) -> list[dict]:
    return [_row(r) for r in records]


# -- adapter ---------------------------------------------------------------------------------------


class PostgresCRM:
    # Customers / identity ---------------------------------------------------------------------------

    async def find_customer_by_phone(self, phone: str) -> dict | None:
        """Only what's safe to know before the caller is verified."""
        async with db.pool().acquire() as c:
            return _row(
                await c.fetchrow(
                    """SELECT cu.id, cu.full_name AS name, cu.status, p.name AS plan
                       FROM customers cu LEFT JOIN plans p ON p.id = cu.plan_id
                       WHERE cu.phone = $1""",
                    normalize_phone(phone),
                )
            )

    async def verify_customer(self, customer_id: int, *, pin: str | None = None, date_of_birth: date | None = None) -> bool:
        async with db.pool().acquire() as c:
            row = await c.fetchrow("SELECT pin_hash, date_of_birth FROM customers WHERE id = $1", customer_id)
        if row is None:
            return False
        if pin is not None:
            return check_pin(pin, row["pin_hash"])
        if date_of_birth is not None:
            return row["date_of_birth"] is not None and row["date_of_birth"] == date_of_birth
        return False

    async def billing_summary(self, customer_id: int) -> dict:
        async with db.pool().acquire() as c:
            account = await c.fetchrow(
                """SELECT cu.status, cu.balance_due, cu.next_bill_date, cu.autopay,
                          p.name AS plan, p.monthly_price
                   FROM customers cu LEFT JOIN plans p ON p.id = cu.plan_id WHERE cu.id = $1""",
                customer_id,
            )
            last = await c.fetchrow(
                "SELECT amount, method, paid_on FROM payments WHERE customer_id = $1 ORDER BY paid_on DESC LIMIT 1",
                customer_id,
            )
        summary = _row(account) or {}
        summary["last_payment"] = _row(last)
        return summary

    # Orders -----------------------------------------------------------------------------------------

    _ORDER_COLUMNS = "id, item, total, status, carrier, tracking_number, ordered_on, shipped_on, eta, delivered_on"

    async def recent_orders(self, customer_id: int, limit: int = 5) -> list[dict]:
        async with db.pool().acquire() as c:
            return _rows(
                await c.fetch(
                    f"SELECT {self._ORDER_COLUMNS} FROM orders WHERE customer_id = $1 ORDER BY ordered_on DESC LIMIT $2",
                    customer_id,
                    limit,
                )
            )

    async def get_order(self, order_id: str, customer_id: int) -> dict | None:
        async with db.pool().acquire() as c:
            return _row(
                await c.fetchrow(
                    f"SELECT {self._ORDER_COLUMNS} FROM orders WHERE regexp_replace(UPPER(id), '[^A-Z0-9]', '', 'g') = regexp_replace(UPPER($1), '[^A-Z0-9]', '', 'g') AND customer_id = $2",
                    order_id,
                    customer_id,
                )
            )

    async def cancel_order(self, order_id: str, customer_id: int) -> dict:
        """Only orders that haven't shipped can be cancelled."""
        async with db.pool().acquire() as c:
            row = await c.fetchrow(
                """UPDATE orders SET status = 'cancelled'
                   WHERE regexp_replace(UPPER(id), '[^A-Z0-9]', '', 'g') = regexp_replace(UPPER($1), '[^A-Z0-9]', '', 'g') AND customer_id = $2 AND status = 'processing'
                   RETURNING id, item, total""",
                order_id,
                customer_id,
            )
            if row:
                return {"cancelled": True, **_row(row), "refund": "Refunded to the original payment method in 3 to 5 business days."}
            current = await c.fetchrow(
                "SELECT status FROM orders WHERE regexp_replace(UPPER(id), '[^A-Z0-9]', '', 'g') = regexp_replace(UPPER($1), '[^A-Z0-9]', '', 'g') AND customer_id = $2",
                order_id,
                customer_id,
            )
        if current is None:
            return {"cancelled": False, "reason": "No order with that number on this account."}
        return {"cancelled": False, "reason": f"The order is {current['status']}; only orders still processing can be cancelled. Shipped orders can be returned instead."}

    # Store appointments -----------------------------------------------------------------------------

    async def list_stores(self) -> list[dict]:
        async with db.pool().acquire() as c:
            return _rows(await c.fetch("SELECT id, name, address, opens_at, closes_at, open_days FROM stores ORDER BY name"))

    async def available_slots(self, day: date, now: datetime, store_id: str | None = None, per_store: int = 6) -> list[dict]:
        """Free 30-minute slots on `day` (store-local), at least an hour from `now`."""
        async with db.pool().acquire() as c:
            stores = await c.fetch(
                "SELECT id, name, opens_at, closes_at, open_days FROM stores WHERE $1::text IS NULL OR id = $1 ORDER BY name",
                store_id,
            )
            taken = {
                (r["store_id"], r["starts_at"])
                for r in await c.fetch(
                    "SELECT store_id, starts_at FROM appointments WHERE status = 'booked' AND starts_at::date = $1", day
                )
            }
        earliest = now.replace(tzinfo=None) + timedelta(hours=1)
        result = []
        for s in stores:
            if day.isoweekday() not in s["open_days"]:
                continue
            slots = []
            t = datetime.combine(day, s["opens_at"])
            close = datetime.combine(day, s["closes_at"])
            while t + timedelta(minutes=SLOT_MINUTES) <= close and len(slots) < per_store:
                if t >= earliest and (s["id"], t) not in taken:
                    slots.append(t.strftime("%Y-%m-%d %H:%M"))
                t += timedelta(minutes=SLOT_MINUTES)
            if slots:
                result.append({"store_id": s["id"], "store": s["name"], "slots": slots})
        return result

    async def book_appointment(self, customer_id: int, store_id: str, starts_at: datetime, reason: str, now: datetime) -> dict:
        free = await self.available_slots(starts_at.date(), now, store_id, per_store=100)
        if not any(starts_at.strftime("%Y-%m-%d %H:%M") in s["slots"] for s in free):
            raise SlotUnavailable("That time isn't available.")
        try:
            async with db.pool().acquire() as c:
                row = await c.fetchrow(
                    """INSERT INTO appointments (customer_id, store_id, starts_at, reason) VALUES ($1, $2, $3, $4)
                       RETURNING id, store_id, starts_at, reason""",
                    customer_id,
                    store_id,
                    starts_at,
                    reason,
                )
        except asyncpg.UniqueViolationError:
            raise SlotUnavailable("That slot was just taken.") from None
        return _row(row)

    async def upcoming_appointments(self, customer_id: int, now: datetime) -> list[dict]:
        async with db.pool().acquire() as c:
            return _rows(
                await c.fetch(
                    """SELECT a.id, s.name AS store, s.address, a.starts_at, a.reason
                       FROM appointments a JOIN stores s ON s.id = a.store_id
                       WHERE a.customer_id = $1 AND a.status = 'booked' AND a.starts_at >= $2
                       ORDER BY a.starts_at""",
                    customer_id,
                    now.replace(tzinfo=None),
                )
            )

    async def cancel_appointment(self, appointment_id: int, customer_id: int) -> bool:
        async with db.pool().acquire() as c:
            result = await c.execute(
                "UPDATE appointments SET status = 'cancelled' WHERE id = $1 AND customer_id = $2 AND status = 'booked'",
                appointment_id,
                customer_id,
            )
        return result.endswith(" 1")

    # Tickets and callbacks ----------------------------------------------------------------------------

    async def create_ticket(
        self, customer_id: int | None, caller_phone: str, call_id: str, subject: str, description: str, priority: str
    ) -> int:
        async with db.pool().acquire() as c:
            return await c.fetchval(
                """INSERT INTO tickets (customer_id, caller_phone, call_id, subject, description, priority)
                   VALUES ($1, $2, $3, $4, $5, $6) RETURNING id""",
                customer_id,
                caller_phone,
                call_id,
                subject,
                description,
                priority,
            )

    async def schedule_callback(
        self, customer_id: int | None, phone: str, call_id: str, preferred_time: str, reason: str
    ) -> int:
        async with db.pool().acquire() as c:
            return await c.fetchval(
                """INSERT INTO callbacks (customer_id, phone, call_id, preferred_time, reason)
                   VALUES ($1, $2, $3, $4, $5) RETURNING id""",
                customer_id,
                phone,
                call_id,
                preferred_time,
                reason,
            )


_crm: PostgresCRM | None = None


def get_crm() -> PostgresCRM:
    global _crm
    if _crm is None:
        _crm = PostgresCRM()
    return _crm
