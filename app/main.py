"""RingMind server entry point: `uvicorn app.main:app` or `python -m app.main`."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse, RedirectResponse

from . import admin, calls, db
from .agent.llm import CALL_CONTROL_LINES, FILLERS, get_pool
from .config import get_settings
from .demo_data import seed
from .speech.tts import DeepgramTTS
from .speech.tts import close_shared as close_tts
from .telephony import asterisk, browser, twilio
from .tools import knowledge

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per LLM/TTS request is noise
log = logging.getLogger("ringmind")


async def _connect_db(dsn: str, attempts: int = 15) -> None:
    for i in range(1, attempts + 1):
        try:
            await db.init_pool(dsn)
            return
        except (OSError, Exception) as e:  # DB container may still be starting
            if i == attempts:
                raise RuntimeError(f"Could not connect to the database: {e}") from e
            log.warning("Database not ready (%s); retrying in 2 s", type(e).__name__)
            await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    await _connect_db(s.database_url)
    if s.seed_demo_data:
        async with db.pool().acquire() as conn:
            if await seed(conn):
                log.info("Loaded demo company data into an empty database")

    if not s.deepgram_api_key:
        log.warning("DEEPGRAM_API_KEY is not set (calls will fail)")
    models = get_pool().active_labels
    if models:
        log.info("LLM pool (in priority order): %s", ", ".join(models))
    else:
        log.warning("No LLM provider key set; add GROQ_API_KEY (or another) to .env")
    if not s.admin_password:
        log.info("Admin dashboard disabled (set ADMIN_PASSWORD to enable /admin)")

    await asyncio.to_thread(knowledge.ensure_ingested, s.kb_docs_path)
    await asyncio.to_thread(knowledge.warm_up)
    if s.deepgram_api_key:
        await DeepgramTTS(s).prewarm(FILLERS + list(CALL_CONTROL_LINES.values()) + ["Are you still there?"])
    server = await asterisk.start_audiosocket_server(s.audiosocket_port) if s.audiosocket_port else None
    log.info("%s is ready to take calls", s.bot_name)
    yield
    if server:
        server.close()
    await calls.close_all()
    await close_tts()
    await db.close_pool()


app = FastAPI(title="RingMind", lifespan=lifespan, docs_url=None, redoc_url=None)
app.include_router(twilio.router)
app.include_router(asterisk.router)
app.include_router(browser.router)
app.include_router(admin.router)


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse("/talk")


@app.get("/health")
async def health() -> dict:
    """Liveness: the process is up."""
    return {"ok": True}


@app.get("/ready")
async def ready() -> JSONResponse:
    """Readiness: can this instance take a call right now?"""
    s = get_settings()
    checks = {
        "database": await db.ping(),
        "llm_models": bool(get_pool().active_labels),
        "speech": bool(s.deepgram_api_key),
        "capacity": calls.active_count() < s.max_concurrent_calls,
    }
    ok = all(checks.values())
    return JSONResponse({"ready": ok, "checks": checks, "active_calls": calls.active_count()}, status_code=200 if ok else 503)


if __name__ == "__main__":
    import uvicorn

    s = get_settings()
    uvicorn.run("app.main:app", host=s.host, port=s.port)
