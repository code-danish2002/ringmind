"""Demo company data ("Nimbus Mobile") for the business tables.

Dates are relative to today, so orders are always "arriving in two days" whenever you seed.
Demo PINs and birthdays are listed in the README so testers can pass identity checks.
"""

from datetime import date, datetime, timedelta

import asyncpg

from .tools.crm import hash_pin, normalize_phone

PLANS = [
    ("basic", "Basic", 29.99, "10 GB data, unlimited calls and texts"),
    ("plus", "Plus", 49.99, "50 GB data, 10 GB hotspot, HD streaming, international texting"),
    ("unlimited", "Unlimited", 69.99, "Unlimited data, 30 GB hotspot, 4K streaming, calls to Canada and Mexico"),
]

# name, phone, email, plan, status, balance, days to next bill, autopay, date of birth, PIN
CUSTOMERS = [
    ("Priya Sharma", "+15551234567", "priya@example.com", "plus", "active", 49.99, 12, False, date(1990, 4, 12), "1234"),
    ("Marcus Lee", "+15559876543", "marcus@example.com", "unlimited", "active", 0.00, 4, True, date(1985, 9, 30), "4321"),
    ("Elena Garcia", "+15555550123", "elena@example.com", "basic", "suspended", 89.97, -35, False, date(1978, 1, 22), "2468"),
    ("Arjun Mehta", "+919876543210", "arjun@example.com", "unlimited", "active", 0.00, 20, True, date(1993, 7, 3), "1357"),
    ("Sofia Rossi", "+15553334444", "sofia@example.com", "plus", "active", 12.50, 8, False, date(2000, 12, 1), "9090"),
]

STORES = [
    ("downtown", "Nimbus Downtown", "120 Main Street", "09:00", "19:00", [1, 2, 3, 4, 5, 6]),
    ("westfield", "Nimbus Westfield Mall", "Westfield Mall, level 2", "10:00", "21:00", [1, 2, 3, 4, 5, 6, 7]),
]

# order id, customer index, item, total, status, carrier, tracking, ordered (days ago), shipped (days ago), eta (days ahead), delivered (days ago)
ORDERS = [
    ("ORD-1042", 0, "Pixel 10 Pro, 256 GB", 999.00, "shipped", "UPS", "1Z999AA10123456784", 4, 2, 2, None),
    ("ORD-1038", 0, "Wireless earbuds", 129.00, "delivered", "USPS", "9400111202555842332669", 20, 18, None, 15),
    ("ORD-1055", 0, "Phone case and screen protector", 39.98, "processing", None, None, 1, None, 5, None),
    ("ORD-1051", 1, "iPhone 18 case", 49.00, "processing", None, None, 0, None, 4, None),
    ("ORD-1033", 1, "iPhone 18 Pro, 512 GB", 1399.00, "delivered", "FedEx", "771234567890", 30, 28, None, 26),
    ("ORD-1047", 2, "SIM card replacement", 0.00, "delayed", "USPS", "9400111202555842339999", 6, 5, 3, None),
    ("ORD-1060", 3, "Galaxy S26, 256 GB", 899.00, "shipped", "DHL", "JD014600006281234567", 3, 1, 3, None),
    ("ORD-1061", 4, "Smartwatch band", 29.00, "processing", None, None, 2, None, 6, None),
]

# customer index, amount, method, days ago
PAYMENTS = [
    (0, 49.99, "card ending 4242", 18),
    (1, 69.99, "AutoPay, bank account", 26),
    (2, 29.99, "card ending 1111", 95),
    (3, 69.99, "AutoPay, card ending 5555", 10),
    (4, 37.49, "card ending 0005", 22),
]


async def seed(conn: asyncpg.Connection, *, reset: bool = False, owner_phone: str | None = None) -> bool:
    """Insert demo data. Returns False (and does nothing) if customers already exist, unless reset.

    `owner_phone` gives the first demo customer (Priya) your own number, so the bot recognises
    you when you call from it.
    """
    async with conn.transaction():
        if reset:
            await conn.execute(
                "TRUNCATE call_events, calls, callbacks, tickets, appointments, orders, payments, customers, stores, plans "
                "RESTART IDENTITY CASCADE"
            )
        elif await conn.fetchval("SELECT count(*) FROM customers"):
            return False

        today = date.today()
        await conn.executemany(
            "INSERT INTO plans (id, name, monthly_price, summary) VALUES ($1, $2, $3, $4) ON CONFLICT (id) DO NOTHING",
            PLANS,
        )
        await conn.executemany(
            """INSERT INTO stores (id, name, address, opens_at, closes_at, open_days)
               VALUES ($1, $2, $3, $4::text::time, $5::text::time, $6) ON CONFLICT (id) DO NOTHING""",
            STORES,
        )
        ids = []
        for i, (name, phone, email, plan, status, balance, bill_days, autopay, dob, pin) in enumerate(CUSTOMERS):
            if i == 0 and owner_phone:
                phone = normalize_phone(owner_phone)
            ids.append(
                await conn.fetchval(
                    """INSERT INTO customers (full_name, phone, email, plan_id, status, balance_due, next_bill_date,
                                              autopay, date_of_birth, pin_hash)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING id""",
                    name, phone, email, plan, status, balance, today + timedelta(days=bill_days), autopay, dob,
                    hash_pin(pin, iterations=50_000),  # lower cost keeps seeding fast; verify reads iterations from the hash
                )
            )

        def ago(days):
            return None if days is None else today - timedelta(days=days)

        def ahead(days):
            return None if days is None else today + timedelta(days=days)

        await conn.executemany(
            """INSERT INTO orders (id, customer_id, item, total, status, carrier, tracking_number,
                                   ordered_on, shipped_on, eta, delivered_on)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)""",
            [
                (oid, ids[ci], item, total, status, carrier, tracking, ago(o), ago(s), ahead(eta), ago(d))
                for oid, ci, item, total, status, carrier, tracking, o, s, eta, d in ORDERS
            ],
        )
        await conn.executemany(
            "INSERT INTO payments (customer_id, amount, method, paid_on) VALUES ($1, $2, $3, $4)",
            [(ids[ci], amount, method, ago(days)) for ci, amount, method, days in PAYMENTS],
        )
        # Marcus already has a store visit booked for the day after tomorrow at 11:00.
        visit_day = today + timedelta(days=2)
        if visit_day.isoweekday() == 7:
            visit_day += timedelta(days=1)
        await conn.execute(
            "INSERT INTO appointments (customer_id, store_id, starts_at, reason) VALUES ($1, 'downtown', $2, $3)",
            ids[1],
            datetime(visit_day.year, visit_day.month, visit_day.day, 11),
            "Trade-in appraisal",
        )
    return True
