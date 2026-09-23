from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


def prepare_database_path(url):
    parsed = make_url(url)
    if parsed.drivername.startswith("sqlite") and parsed.database not in (None, "", ":memory:"):
        Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)


def database(url):
    prepare_database_path(url)
    engine = create_async_engine(url, echo=False)
    if url.startswith("sqlite"):

        @event.listens_for(engine.sync_engine, "connect")
        def sqlite_pragmas(dbapi_connection, connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            # A report can persist a large snapshot while an administrator
            # changes goals. Give SQLite enough time to hand the single writer
            # slot to the next transaction instead of failing the menu action.
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

    return engine, async_sessionmaker(engine, expire_on_commit=False)
