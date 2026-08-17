"""Alembic environment.

The database URL comes from ``config.yaml`` (or ``$YT_TX_TEST_DSN``, so the test
suite can migrate a disposable database) rather than from ``alembic.ini``, which
keeps credentials out of version control.

There is no SQLAlchemy metadata to autogenerate against - the schema is literal
DDL in :mod:`yt_tx.db`, which is deliberate. Migrations are hand-written.
"""

from __future__ import annotations

from alembic import context

from yt_tx import db as ytdb
from yt_tx.settings import load_bootstrap

config = context.config


def _url() -> str:
    override = config.get_main_option("sqlalchemy.url")
    if override:
        return override
    test = ytdb.test_dsn()
    if test:
        return test
    return load_bootstrap().mysql.dsn()


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=None,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = ytdb.engine_from_url(_url(), pool_size=1)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=None)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
