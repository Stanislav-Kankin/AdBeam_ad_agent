import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import httpx
import pytest
from aiogram import Bot
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, select
from test_bot_schedule import FakeTelegram, message_update

from app.agent.service import AgentService
from app.bot.handlers import build_dispatcher
from app.config import load_clients
from app.domain.clients import DirectConfig, MetricaConfig, RevenueConfig
from app.integrations.deepseek import LLMMessage
from app.integrations.discovery import AccountDiscovery
from app.integrations.http import ReadTransport
from app.storage.models import Delivery, Run, ToolEvent


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Обычное сообщение коллегам", False),
        ("@adbeam_test_bot_fake вопрос", False),
        ("@adbeam_test_bot вопрос", True),
    ],
)
async def test_group_messages_require_address(runtime, text, expected):
    runtime.agent.ask = AsyncMock(return_value="Ответ")
    update = message_update(text)
    update = update.model_copy(
        update={
            "message": update.message.model_copy(
                update={"chat": update.message.chat.model_copy(update={"type": "supergroup"})}
            )
        }
    )
    async with Bot(token="555:SYNTHETIC_TEST_TOKEN", session=FakeTelegram()) as bot:
        await build_dispatcher(runtime).feed_update(bot, update)
        await runtime.jobs.close()
    assert runtime.agent.ask.called == expected


async def test_optional_user_allowlist_blocks_commands(runtime):
    runtime.settings.telegram_allowed_user_ids = [99]
    session = FakeTelegram()
    async with Bot(token="555:SYNTHETIC_TEST_TOKEN", session=session) as bot:
        await build_dispatcher(runtime).feed_update(bot, message_update("/check West"))
    assert not session.sent


async def test_manual_run_and_tools_record_actor(runtime):
    await runtime.agent.ask("Почему у West Экспорт вырос CPA за 7 дней?", 123456789, 42)
    async with runtime.checks.repository.sessions() as session:
        runs = (await session.scalars(select(Run))).all()
        events = (await session.scalars(select(ToolEvent))).all()
    assert runs and events
    assert all(r.user_id == "42" for r in runs)
    assert all(e.user_id == "42" for e in events)


async def test_discovery_merges_overrides_and_skips_bad_row(settings, tmp_path, monkeypatch):
    settings.app_mode = "production"
    settings.clients_config = tmp_path / "clients.yaml"
    settings.clients_config.write_text(
        """clients:
  - direct:
      client_login: valid-login
      main_goal_ids: ['42']
    metrica:
      counter_id: 6754453
      main_goal_ids: ['42']
    targets:
      target_cpa: 500
    telegram:
      allowed_chat_ids: [999]
  - direct:
      client_login: not-in-account
""",
        encoding="utf-8",
    )
    registry = load_clients(settings)
    assert not registry.clients
    settings.yandex_client_chat_ids = [123456789]
    monkeypatch.setenv("DIRECT_OAUTH_TOKEN", "synthetic")
    payload = {"result": {"Clients": [{"Login": "valid-login"}, {"Login": "invalid login"}, {}]}}
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload))
    ) as http:
        service = AccountDiscovery(ReadTransport(http), registry, settings)
        assert await service.refresh() == 1
        await service.refresh()
    client = next(iter(registry.clients.values()))
    assert client.metrica.counter_id == 6754453
    assert client.direct.main_goal_ids == ["42"]
    assert client.targets.target_cpa == 500
    assert client.telegram.allowed_chat_ids == [123456789]


@pytest.mark.parametrize(
    "cls,kwargs",
    [(DirectConfig, {"client_login": "test"}), (MetricaConfig, {}), (RevenueConfig, {})],
)
def test_token_env_cannot_reference_unrelated_secret(cls, kwargs):
    for name in ("PATH", "TELEGRAM_BOT_TOKEN", "DATABASE_URL", "ADBEAM_PASSWORD"):
        with pytest.raises(ValidationError):
            cls(**kwargs, token_env=name)


async def test_model_quota_survives_service_restart(runtime):
    llm = AsyncMock()
    llm.complete.return_value = LLMMessage(content="Ответ")
    first = AgentService(runtime.checks, llm, daily_limit=1)
    assert await first.ask("Привет", 123456789, 1) == "🧪 MOCK — тестовые данные\nОтвет"
    second = AgentService(runtime.checks, llm, daily_limit=1)
    assert "суточный лимит" in await second.ask("Привет", 123456789, 1)
    assert llm.complete.await_count == 1


async def test_model_quota_concurrent_reservations(runtime):
    results = await asyncio.gather(
        *(runtime.checks.repository.reserve_model_call(123456789, i, str(i), 2) for i in range(6))
    )
    assert sum(results) == 2


async def test_retention_keeps_other_mode_and_pending_delivery(runtime):
    old = datetime.now(UTC) - timedelta(days=100)
    repo = runtime.checks.repository
    async with repo.sessions.begin() as session:
        for mode in ("mock", "production"):
            session.add(
                ToolEvent(
                    request_id=mode,
                    app_mode=mode,
                    chat_id="1",
                    tool="test",
                    arguments={},
                    status="ok",
                    duration_seconds=0,
                    created_at=old,
                )
            )
        session.add(Delivery(key="mock:sent", status="sent", parts=[], updated_at=old))
        session.add(
            Delivery(key="mock:pending", status="pending", parts=["report"], updated_at=old)
        )
    await repo.purge(90)
    async with repo.sessions() as session:
        events = (await session.scalars(select(ToolEvent))).all()
        deliveries = (await session.scalars(select(Delivery))).all()
    assert [e.app_mode for e in events] == ["production"]
    assert [d.key for d in deliveries] == ["mock:pending"]


async def test_offline_reports_keep_polling_and_honor_retryin():
    codes = iter([201] + [202] * 5 + [200])
    sleep = AsyncMock()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(next(codes), headers={"retryIn": "75"})
        )
    ) as http:
        response = await ReadTransport(http, sleep=sleep).request(
            "direct", "POST", "https://example.org/reports", pending=True
        )
    assert response.status_code == 200
    assert sleep.await_count == 6
    assert all(call.args == (75,) for call in sleep.await_args_list)


def test_migration_preserves_old_rows(tmp_path):
    path = tmp_path / "upgrade.db"
    config = Config("alembic.ini")
    config.attributes["database_url"] = "sqlite+aiosqlite:///" + str(path)
    command.upgrade(config, "ad077c7f4012")
    engine = create_engine("sqlite:///" + str(path))
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "INSERT INTO tool_events (request_id,app_mode,chat_id,tool,arguments,status,duration_seconds,created_at) VALUES ('old','production','1','test','{}','ok',0,'2026-09-01')"
        )
    command.upgrade(config, "head")
    assert "user_id" in {c["name"] for c in inspect(engine).get_columns("tool_events")}
    with engine.connect() as conn:
        assert conn.exec_driver_sql("SELECT request_id,user_id FROM tool_events").one() == (
            "old",
            None,
        )
    engine.dispose()
