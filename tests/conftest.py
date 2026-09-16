from pathlib import Path

import pytest

from app.config import Settings
from app.runtime import build_runtime
from app.storage.models import Base


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        app_mode="mock",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        clients_config=Path("config/clients.example.yaml"),
        telegram_allowed_chat_ids=[123456789],
        telegram_admin_user_ids=[1],
        telegram_report_chat_id=123456789,
        schedule_enabled=False,
        deepseek_api_key="",
        mock_schedule_interval_seconds=0,
    )


@pytest.fixture
async def runtime(settings):
    runtime = build_runtime(settings)
    async with runtime.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield runtime
    await runtime.close()


@pytest.fixture
def client(runtime):
    return runtime.registry.clients["west_export"]
