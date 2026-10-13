"""Engine, session factory and declarative base for the agent's database.

Three deliberate choices:

* **A naming convention is set on the metadata.** Without one, SQLite invents
  constraint names, and a migration that later needs to drop or alter a constraint
  has nothing stable to name. Fixing names now costs nothing and makes every future
  migration writable.
* **SQLite gets pragmas.** ``foreign_keys=ON`` is off by default in SQLite, so a
  foreign key that "works" in tests against PostgreSQL would silently do nothing
  here. WAL is enabled because the agent writes traces while requests read.
* **Timestamps are UTC and timezone-aware.** Everything the agent stores is compared
  across processes and printed in traces; a naive local timestamp makes "expired"
  depend on the reader's clock.

The session factory is created once per engine. ``session_scope`` is the only
intended way to get a session, so commit/rollback handling is not repeated at every
call site.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import Engine, MetaData, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from agent_service.config import Settings, get_settings

#: Stable names for every constraint, so migrations can reference them.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base shared by every model.

    Alembic's autogenerate compares against ``Base.metadata``, so a model that is
    never imported is a model whose table is never created. ``db/models/__init__.py``
    imports all of them for exactly that reason.
    """

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utc_now() -> datetime:
    """A timezone-aware UTC timestamp.

    Used as the default for every timestamp column, so stored values are comparable
    across processes regardless of the host's timezone.
    """
    return datetime.now(UTC)


def _sqlite_connect_args(settings: Settings) -> dict[str, object]:
    """Connect arguments SQLite needs when used from more than one thread.

    FastAPI runs sync handlers in a thread pool, so a connection created on one
    thread is legitimately used on another. The alternative — a connection per
    request — would give up WAL's benefit for no gain here.
    """
    if not settings.is_sqlite:
        return {}
    return {"check_same_thread": False}


def register_sqlite_pragmas(engine: Engine) -> None:
    """Turn on foreign keys and WAL for every SQLite connection.

    SQLite ignores foreign keys unless asked, which would make this schema's
    cascade deletes and referential guarantees untestable and untrue.
    """

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection: object, _record: object) -> None:  # pragma: no cover
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


def create_db_engine(settings: Settings | None = None) -> Engine:
    """Build an engine for the configured database."""
    resolved = settings or get_settings()
    engine = create_engine(
        resolved.database_url,
        connect_args=_sqlite_connect_args(resolved),
        # Recycle before a database or proxy drops an idle connection.
        pool_pre_ping=True,
        future=True,
    )
    if resolved.is_sqlite:
        register_sqlite_pragmas(engine)
    return engine


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """A session factory bound to ``engine``.

    ``expire_on_commit=False`` because the agent reads attributes off ORM objects
    after committing — to build an HTTP response, for example — and re-fetching them
    would mean an extra round trip per request for no benefit.
    """
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """A session with commit-on-success and rollback-on-failure.

    The one place transaction boundaries are decided. A repository never commits;
    the caller of ``session_scope`` decides when a unit of work is complete, which is
    what makes a multi-step operation (approve, then execute, then record) atomic.
    """
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
