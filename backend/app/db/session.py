"""SQLite engine/session factory.

* Foreign keys enforced on every connection.
* ``busy_timeout`` so concurrent writers wait instead of failing immediately.
* WAL journal mode for concurrent readers alongside the worker's writes.
* Transactions start with ``BEGIN IMMEDIATE`` so a transaction takes the write
  lock up front; this avoids SQLite's deadlock-prone read->write lock upgrade
  when the API and worker(s) write concurrently.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

BUSY_TIMEOUT_MS = 15000


def make_engine(database_url: str) -> Engine:
    engine = create_engine(
        database_url,
        connect_args={"check_same_thread": False, "timeout": BUSY_TIMEOUT_MS / 1000},
        pool_pre_ping=True,
    )

    @event.listens_for(engine, "connect")
    def _on_connect(dbapi_conn, _record):
        # Hand transaction control to SQLAlchemy (see "begin" hook below).
        dbapi_conn.isolation_level = None
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    @event.listens_for(engine, "begin")
    def _on_begin(conn):
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


class Database:
    """Holds the engine and session factory for one process."""

    def __init__(self, database_url: str):
        self.url = database_url
        self.engine = make_engine(database_url)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Transactional scope: commits on success, rolls back on error."""
        s = self.SessionLocal()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def dispose(self) -> None:
        self.engine.dispose()
