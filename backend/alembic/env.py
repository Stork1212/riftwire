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


def run_migrations_online() -> None:
    engine = create_engine(_url())

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _rec):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          render_as_batch=True, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
