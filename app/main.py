from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import BASE_DIR, DATA_DIR, DEBUG, RUN_AGENT, RUN_BOT, RUN_SCHEDULER
from .database import init_db
from .pages import router as pages_router
from .routers import agent_files, agents, api_v1, auth, dashboard, folders, integrations, jobs, logs, miniapp, nvrs, users
from .services.health import start_scheduler, stop_scheduler
from .services.jobs import init_job_manager, shutdown_job_manager
from .settings_service import seed_database

class _RedactingFormatter(logging.Formatter):
    """Strip Telegram bot tokens from anything that reaches the log."""

    _token = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}\b")

    def format(self, record: logging.LogRecord) -> str:
        return self._token.sub("<redacted-telegram-token>", super().format(record))


_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
logging.basicConfig(level=logging.DEBUG if DEBUG else logging.INFO, format=_LOG_FORMAT)
for _handler in logging.getLogger().handlers:
    _handler.setFormatter(_RedactingFormatter(_LOG_FORMAT))

# Keep a rotating tail on disk so Settings -> Logs can show recent warnings and
# errors without needing `docker logs`. The redacting formatter is reused.
LOG_DIR = DATA_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "cctv.log"


def _install_file_log() -> None:
    root = logging.getLogger()
    target = str(LOG_FILE)
    for handler in root.handlers:
        if isinstance(handler, RotatingFileHandler) and getattr(handler, "baseFilename", "") == target:
            return
    handler = RotatingFileHandler(
        target,
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(_RedactingFormatter(_LOG_FORMAT))
    root.addHandler(handler)


_install_file_log()
# Third-party libraries log full request URLs at DEBUG. Telegram puts the bot
# token in the URL and Google/Drive calls can include signed parameters, so
# keep those loggers at WARNING even when app DEBUG is enabled.
for _noisy in (
    "urllib3",
    "requests",
    "httpx",
    "httpcore",
    "telegram",
    "TeleBot",
    "googleapiclient",
    "google.auth",
):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
logger = logging.getLogger("cctv")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    seed_database()
    init_job_manager()
    logger.info("CCTV V3 database ready")

    if RUN_AGENT:
        # Agent containers must not run a second bot/scheduler against the same
        # Telegram token; they only poll the main host.
        from .agent_runner import start_agent_runner

        if start_agent_runner():
            logger.info("Campus agent runner started")
    else:
        if RUN_SCHEDULER:
            start_scheduler()
            logger.info("Health scheduler started")
        if RUN_BOT:
            try:
                from .bot import start_bot

                start_bot()
                logger.info("Telegram bot thread started")
            except Exception as exc:
                logger.error("Telegram bot failed to start: %s", exc)
    yield
    try:
        from .bot import stop_bot

        stop_bot()
    except Exception:
        pass
    if RUN_AGENT:
        try:
            from .agent_runner import stop_agent_runner

            stop_agent_runner()
        except Exception:
            pass
    stop_scheduler()
    shutdown_job_manager()
    logger.info("CCTV V3 stopped")


app = FastAPI(
    title="CCTV Control Center",
    version="3.0.0",
    docs_url="/api/docs" if DEBUG else None,
    redoc_url=None,
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "app" / "static")), name="static")

app.include_router(auth.router)
app.include_router(users.router)
app.include_router(integrations.router)
app.include_router(folders.router)
app.include_router(nvrs.router)
app.include_router(jobs.router)
app.include_router(dashboard.router)
app.include_router(agents.router)
app.include_router(agent_files.router)
app.include_router(logs.router)
app.include_router(api_v1.router)
app.include_router(miniapp.router)
app.include_router(pages_router)


@app.get("/healthz", include_in_schema=False)
def healthz():
    return {"ok": True, "version": "3.0.0"}


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception):
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    if request.url.path.startswith("/api/"):
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})
    return JSONResponse(status_code=500, content={"detail": "Internal server error"})
