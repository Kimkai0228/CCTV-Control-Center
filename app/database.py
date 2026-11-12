"""SQLAlchemy setup for the CCTV V3 control plane."""

from __future__ import annotations

import logging
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import DATABASE_URL, DATA_DIR

Base = declarative_base()

_is_sqlite = DATABASE_URL.startswith("sqlite")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False, "timeout": 30} if _is_sqlite else {},
    pool_pre_ping=True,
    future=True,
)

if _is_sqlite:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_connection, _connection_record):  # pragma: no cover - driver hook
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)


@contextmanager
def session_scope():
    """Transactional session for background workers."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db():
    """FastAPI dependency."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from . import models  # noqa: F401 - register metadata

    # Make sure the data dir exists before SQLite creates the file.
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)
    _ensure_sqlite_columns()
    _drop_legacy_columns()


logger = logging.getLogger("cctv.database")

# Additive SQLite migrations for databases created by earlier v3 builds.
# create_all() creates new tables but never alters existing ones.
_ADDITIVE_COLUMNS: dict[str, dict[str, str]] = {
    "alerts": {
        "last_notified_at": "DATETIME",
        "resolve_note": "TEXT",
    },
    "cameras": {
        "ip_address": "VARCHAR(64)",
        "mac_address": "VARCHAR(64)",
        "enabled": "BOOLEAN NOT NULL DEFAULT 1",
    },
    "jobs": {
        "agent_id": "INTEGER",
        "dispatched_at": "DATETIME",
        "retry_json": "TEXT",
    },
    "agents": {
        "public_url": "VARCHAR(400)",
    },
    "audit_logs": {
        "level": "VARCHAR(20)",
    },
}


def _ensure_sqlite_columns() -> None:
    """Apply small additive migrations without touching existing data."""
    if not _is_sqlite:
        return
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    for table, columns in _ADDITIVE_COLUMNS.items():
        if table not in tables:
            continue
        existing = {column["name"] for column in inspector.get_columns(table)}
        with engine.begin() as connection:
            for name, definition in columns.items():
                if name not in existing:
                    connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {definition}"))


# SNMP / temperature monitoring was removed from v3. Drop the columns from
# databases created by earlier builds so stale schema does not linger.
_LEGACY_COLUMNS: dict[str, tuple[str, ...]] = {
    "nvrs": (
        "temperature_c",
        "snmp_enabled",
        "snmp_version",
        "snmp_community",
        "snmp_port",
        "snmp_temperature_oid",
    ),
}


def _drop_legacy_columns() -> None:
    """Best-effort removal of columns that are no longer part of the schema."""
    if not _is_sqlite:
        return
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    for table, columns in _LEGACY_COLUMNS.items():
        if table not in tables:
            continue
        existing = {column["name"] for column in inspector.get_columns(table)}
        stale = [name for name in columns if name in existing]
        if not stale:
            continue
        with engine.begin() as connection:
            for name in stale:
                try:
                    connection.execute(text(f"ALTER TABLE {table} DROP COLUMN {name}"))
                except Exception as exc:  # older SQLite or dependent index
                    logger.warning(
                        "Could not drop legacy column %s.%s: %s", table, name, exc
                    )
