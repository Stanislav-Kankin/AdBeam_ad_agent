import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import load_settings
from app.storage.database import prepare_database_path
from app.storage.models import Base

url = context.config.attributes.get("database_url") or load_settings().database_url
prepare_database_path(url)


def migrate(connection):
    context.configure(connection=connection, target_metadata=Base.metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def online():
    engine = create_async_engine(url)
    async with engine.connect() as connection:
        await connection.run_sync(migrate)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(online())
