"""Renamed: use `python -m scripts.seed_db` (Postgres). Kept so old instructions still work."""

from scripts.seed_db import main as _main

if __name__ == "__main__":
    import argparse
    import asyncio

    p = argparse.ArgumentParser()
    p.add_argument("--reset", action="store_true")
    p.add_argument("--phone")
    a = p.parse_args()
    asyncio.run(_main(a.reset, a.phone))
