from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, event

from app import models  # noqa: F401  (register models on metadata)
from app.core.config import get_settings
from app.db.base import Base

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _url() -> str:
    url = config.attributes.get("database_url") or config.get_main_option("sqlalchemy.url")
    if url:
        return url
    settings = get_settings()
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    return settings.database_url


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True,
                      render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


class ForeignKeyViolation(RuntimeError):
    """A migration left a row referencing a missing parent; nothing was committed."""


def run_migrations_online() -> None:
    engine = create_engine(_url(), echo=bool(config.attributes.get("echo")))

    # Foreign-key enforcement is OFF while migrating. A batch migration rebuilds a table in
    # SQLite (create copy, copy rows, DROP the original, rename); with enforcement on, that
    # DROP performs an implicit DELETE of every row and fires ON DELETE actions on child
    # tables: rebuilding ``recordings`` deleted every job (CASCADE) and blanked every
    # system-event reference (SET NULL). This follows SQLite's documented procedure for
    # schema changes: disable enforcement, change the schema, then run
    # ``PRAGMA foreign_key_check`` before committing. The application itself always
    # enforces foreign keys (``app.db``).
    #
    # The whole run is one real SQLite transaction, DDL included: the driver's own
    # transaction handling is off (it never opens a transaction for DDL, so schema changes
    # used to commit one by one while the version row could roll back) and BEGIN IMMEDIATE
    # is emitted explicitly (SQLAlchemy's documented pysqlite recipe).
    @event.listens_for(engine, "connect")
    def _connect(dbapi_conn, _rec):
        dbapi_conn.isolation_level = None
        dbapi_conn.execute("PRAGMA foreign_keys=OFF")      # outside any transaction
        if dbapi_conn.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
            raise RuntimeError("foreign-key enforcement must be off while migrating")

    @event.listens_for(engine, "begin")
    def _begin(conn):
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    with engine.connect() as connection:
        # transactional_ddl: Alembic's SQLite default (False) commits after every revision;
        # with the BEGIN above SQLite DDL is transactional, so the run is one transaction.
        context.configure(connection=connection, target_metadata=target_metadata,
                          render_as_batch=True, compare_type=True, transactional_ddl=True)
        with context.begin_transaction():
            context.run_migrations()
            # Same transaction: a violation rolls back every migration step of this run.
            bad = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            if bad:
                raise ForeignKeyViolation(
                    f"migration left {len(bad)} foreign-key violation(s), first: "
                    f"{tuple(bad[0])}; nothing was committed")
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
