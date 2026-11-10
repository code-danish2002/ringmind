"""Load the demo company data into Postgres.

    python -m scripts.seed_db                      # only if the database is empty
    python -m scripts.seed_db --reset              # wipe business data + call history, reseed
    python -m scripts.seed_db --reset --phone +91XXXXXXXXXX   # Priya gets your number

With --phone, calls from your own mobile are recognised as Priya Sharma (PIN 1234).
"""

import argparse
import asyncio

from app import db
from app.config import get_settings
from app.demo_data import seed


async def main(reset: bool, phone: str | None) -> None:
    pool = await db.init_pool(get_settings().database_url)
    async with pool.acquire() as conn:
        done = await seed(conn, reset=reset, owner_phone=phone)
    await db.close_pool()
    print("Seeded demo data." if done else "Database already has customers; use --reset to replace them.")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--reset", action="store_true")
    p.add_argument("--phone", help="Give the first demo customer (Priya) this phone number")
    args = p.parse_args()
    asyncio.run(main(args.reset, args.phone))
