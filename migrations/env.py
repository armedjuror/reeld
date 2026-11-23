"""Alembic environment for the suite tables.

Only tables prefixed `suite_` are managed here. The legacy tables (users,
presets, reels, credit_transactions, generations) are still created by
db.init_db(); the baseline migration only creates `users` if it is missing.
"""

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from suite import models  # noqa: F401  (registers tables on Base.metadata)
from suite.db import Base, database_url

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", database_url().replace("%", "%%"))

target_metadata = Base.metadata


def include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table":
        return name.startswith("suite_")
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        include_object=include_object,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
