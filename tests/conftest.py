import os

import pytest

# Set before app.config is imported; real env vars take precedence over .env.
# Don't bind the real AudioSocket port (a dev server may already be running on it).
os.environ["AUDIOSOCKET_PORT"] = "0"
# Never touch the dev database: tests use their own (docker compose's db, database ringmind_test).
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "postgresql://ringmind:ringmind@localhost:5433/ringmind_test")
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["ADMIN_PASSWORD"] = "test-admin-pw"
# No real speech API calls from tests (e.g. the TTS warm-up at startup).
os.environ["DEEPGRAM_API_KEY"] = ""
os.environ["ADMIN_USER"] = "admin"


@pytest.fixture
async def pg():
    """A freshly seeded test database. Skips the test if Postgres isn't running."""
    import asyncpg

    from app import db
    from app.demo_data import seed

    try:
        await db.init_pool(TEST_DATABASE_URL)
    except (OSError, asyncpg.PostgresError) as e:
        pytest.skip(f"test database unavailable ({e}); run `docker compose up -d db`")
    async with db.pool().acquire() as conn:
        await seed(conn, reset=True)
    yield db.pool()
    await db.close_pool()
