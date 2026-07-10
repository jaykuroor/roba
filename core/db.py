"""Database wiring: engine, session, Base, create_all, get_db, reset_db.

- One SQLite file (path from ``config.DB_PATH``); ``check_same_thread=False``
  so the async app + tick loop can share it; ``pool_pre_ping=True``.
- ``Base`` is the declarative base every model in ``models.py`` inherits from.
- ``reset_db`` is the trivial-reset hook the demo controls use (§6.2): it can
  wipe the transactional + intelligence tables while preserving the seeded
  reference/config (and simulation/control) data, or reset everything.
"""

from contextlib import contextmanager
import threading
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from . import config


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


engine = create_engine(
    f"sqlite:///{config.DB_PATH}",
    connect_args={"check_same_thread": False},
    pool_pre_ping=True,
    # SQLite with WAL + check_same_thread=False is cheap on concurrent handles;
    # raise the pool ceiling so a deep signal cascade (emit → subscriber → emit …)
    # never exhausts the default size-5/overflow-10 = 15-connection limit.
    pool_size=20,
    max_overflow=40,
    pool_recycle=1800,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
)

# Coordinates destructive reseeds, API read bursts, and the tick loop. It is
# deliberately process-local because the demo runs a single uvicorn worker.
DB_LOCK = threading.RLock()


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    """Tune SQLite for the demo's concurrent FastAPI/tick-loop workload."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA journal_mode=WAL")
    finally:
        cursor.close()


def new_session() -> Session:
    """Return one independent ORM session owned by the caller."""
    return SessionLocal()


@contextmanager
def session_scope(coordinated: bool = False) -> Iterator[Session]:
    """Create a session, commit on success, rollback on error, always close.

    ``coordinated=True`` also takes ``DB_LOCK`` for the block. Use that for
    operations that must not interleave with the tick loop or frontend reads.
    """
    lock = DB_LOCK if coordinated else None
    if lock is not None:
        lock.acquire()
    session = new_session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
        if lock is not None:
            lock.release()


def create_all():
    """Create every table defined in ``models.py``."""
    from . import models  # noqa: F401  (import registers all models on Base.metadata)

    Base.metadata.create_all(bind=engine)
    _ensure_columns()


def _ensure_columns() -> None:
    """Idempotent ALTER TABLE for columns added after the initial schema.

    Runs on every startup so existing ``demo.db`` files gain the new columns
    without requiring a full reset.  SQLite silently ignores duplicate-column
    errors via the ``ignore_errors=True`` check.
    """
    _new_cols = [
        # (table, column, ddl_type_default)
        ("suppliers",       "phone",                  "TEXT"),
        ("suppliers",       "delivery_charge",         "REAL DEFAULT 0.0"),
        ("suppliers",       "volume_discount",         "TEXT"),          # JSON stored as TEXT
        ("supplier_catalog","is_default",              "INTEGER DEFAULT 0"),
        ("supplier_catalog","discount",                "TEXT"),
        ("competitors",     "phone",                   "TEXT"),          # for direct-call chooser
        # Restaurant identity on AppSettings (added with identity workstream)
        ("app_settings",    "restaurant_title",        "TEXT"),
        ("app_settings",    "restaurant_location",     "TEXT"),
        ("app_settings",    "restaurant_phone",        "TEXT"),
        # Per-call operator metadata (note, ingredient, intel_goal)
        ("calls",           "call_metadata",           "TEXT"),   # JSON stored as TEXT
        # SupplierTerm columns (added with robust-capture workstream)
        ("supplier_terms",  "unit_basis",              "TEXT"),
        ("supplier_terms",  "manager_change_id",       "INTEGER"),
        # Procurement plan coverage status (added with least-cost/no-stockout fix)
        ("procurement_plan_runs", "coverage_ok",       "INTEGER DEFAULT 1"),
        ("procurement_plan_runs", "total_short",       "REAL DEFAULT 0.0"),
        # PO urgency label carried from the source planned row (at_risk/uncoverable/None)
        ("purchase_orders",       "urgency",            "TEXT"),
    ]
    conn = engine.raw_connection()
    try:
        cur = conn.cursor()
        for table, col, ddl in _new_cols:
            try:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
            except Exception:  # noqa: BLE001  column already exists
                pass
        conn.commit()
    finally:
        conn.close()


def get_db():
    """FastAPI dependency: yield a session and always close it."""
    db = new_session()
    try:
        yield db
    finally:
        db.close()


def reset_db(keep_reference=True):
    """Reset the database.

    ``keep_reference=True`` (default): drop and recreate only the
    transactional (§19.2) + intelligence (§19.3) tables, leaving the
    reference/config (§19.1) and simulation/control (§19.4) tables and their
    seeded data intact.

    ``keep_reference=False``: completely reset — drop and recreate every table.
    """
    from . import models

    # Ensure the schema exists before we attempt selective drops.
    with DB_LOCK:
        Base.metadata.create_all(bind=engine)

        if keep_reference:
            tables = [
                m.__table__
                for m in (models.TRANSACTIONAL_MODELS + models.INTELLIGENCE_MODELS)
            ]
            Base.metadata.drop_all(bind=engine, tables=tables)
            Base.metadata.create_all(bind=engine, tables=tables)
        else:
            Base.metadata.drop_all(bind=engine)
            Base.metadata.create_all(bind=engine)
