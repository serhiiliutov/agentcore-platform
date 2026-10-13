"""Alembic environment for the agent service.

Two things this file deliberately does NOT do:

* **It does not read a URL from ``alembic.ini``.** The URL comes from
  ``agent_service.config``, which reads the environment. That means a migration and
  the running service can never disagree about which database they mean — a class of
  mistake that usually shows up as "the migration applied, but the app still fails".
* **It does not import models by path.** It imports ``agent_service.db.models``,
  which imports every model, which is what populates ``Base.metadata``. Adding a model
  and forgetting to register it is therefore impossible: registering it is what the
  module does.

``compare_type`` and ``compare_server_default`` are on so a changed column type is
detected. Off, autogenerate silently reports "no changes" while the database and the
models have quietly diverged.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import Engine

# Importing the package is what registers every table on Base.metadata.
import agent_service.db.models  # noqa: F401  (imported for its side effect)
from agent_service.config import get_settings
from agent_service.db.base import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """The URL the service itself would use."""
    return get_settings().database_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of running it.

    Used to inspect a migration before it touches a database — which is how a
    destructive change gets caught in review rather than in production.
    """
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Apply migrations against a live connection."""
    settings = get_settings()

    # Connect through the service's own engine factory so SQLite gets the same
    # pragmas the application runs with: without `foreign_keys=ON`, a migration that
    # creates a foreign key would build something the app cannot rely on.
    from agent_service.db.base import create_db_engine

    engine: Engine = create_db_engine(settings)

    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
            # SQLite cannot ALTER most things in place; batch mode rebuilds the table,
            # which is the difference between a migration that works and one that
            # fails only on the default configuration.
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
