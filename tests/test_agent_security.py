import json
from unittest.mock import AsyncMock

import pytest
import yaml
from sqlalchemy import select

from app.agent.service import AgentService
from app.agent.tools import DESCRIPTIONS, ToolRegistry
from app.analytics.periods import make_period
from app.config import load_clients
from app.domain.reports import CheckMode, TriggerSource
from app.integrations.deepseek import LLMMessage, ToolCall
from app.security import redact
from app.storage.models import Run, ToolEvent
from app.storage.repository import safe_json


async def test_every_tool_denies_unknown_chat_before_fetch(runtime, monkeypatch):
    provider = AsyncMock(side_effect=AssertionError("must not fetch"))
    monkeypatch.setattr(runtime.checks.provider, "snapshot", provider)
    registry = ToolRegistry(runtime.checks)
    for name in DESCRIPTIONS:
        result = await registry.call(
            name, '{"client_id":"west_export"}', chat_id=999, request_id="r"
        )
        assert result["status"] == "denied"
    provider.assert_not_called()
    with pytest.raises(PermissionError):
        await runtime.checks.run_check(
            ["west_export"], make_period(), CheckMode.STANDARD, TriggerSource.INTERNAL, chat_id=999
        )


async def test_client_permission_each_tool_and_no_cross_chat_leak(runtime):
    runtime.registry.clients["west_export"].telegram.allowed_chat_ids = [888]
    registry = ToolRegistry(runtime.checks)
    for name in DESCRIPTIONS:
        args = "{}" if name == "list_clients" else '{"client_id":"west_export"}'
        result = await registry.call(name, args, chat_id=123456789, request_id="r")
        if name == "list_clients":
            assert "west_export" not in json.dumps(result)
        else:
            assert result["status"] == "denied"


@pytest.mark.parametrize(
    "name,args",
    [
        ("execute_sql", '{"sql":"SELECT * FROM clients"}'),
        ("get_account_overview", '{"client_id":"west_export","period":"91d"}'),
        ("get_revenue", '{"client_id":"west_export","top_n":500}'),
        ("get_revenue", '{"client_id":"west_export","url":"https://evil.invalid"}'),
        ("get_revenue", "{invalid"),
        ("get_revenue", '{"client_id":"west_export","start_date":"2099-01-01"}'),
    ],
)
async def test_tool_parameters_validated_and_audited(runtime, name, args):
    result = await runtime.agent.tools.call(name, args, chat_id=123456789, request_id="test")
    assert result["status"] == "invalid"
    async with runtime.checks.repository.sessions() as session:
        event = await session.scalar(select(ToolEvent))
        assert event.arguments == {}


async def test_multistep_model_and_audit(runtime):
    reply = await runtime.agent.ask("Почему у West Экспорт вырос CPA за 7 дней?", 123456789, 1)
    assert "4 000,00" in reply and "MOCK" in reply
    async with runtime.checks.repository.sessions() as session:
        events = (await session.scalars(select(ToolEvent))).all()
        assert [e.tool for e in events] == ["list_clients", "get_account_overview"]
        run = await session.scalar(select(Run))
        assert run.status == "completed" and run.trigger == "agent"
        assert run.duration_seconds is not None and run.duration_seconds >= 0


async def test_eight_tool_call_budget(runtime):
    class Endless:
        async def complete(self, messages, tools):
            return LLMMessage(calls=[ToolCall(f"c{len(messages)}", "list_clients", "{}")])

    service = AgentService(runtime.checks, Endless())
    result = await service.ask("Все клиенты", 123456789, 1)
    assert "лимит" in result
    async with runtime.checks.repository.sessions() as session:
        assert len((await session.scalars(select(ToolEvent))).all()) == 8


async def test_multiple_tool_calls_cannot_overrun_budget(runtime):
    llm = AsyncMock()
    llm.complete.return_value = LLMMessage(
        calls=[ToolCall(str(i), "list_clients", "{}") for i in range(9)]
    )
    service = AgentService(runtime.checks, llm)
    await service.ask("Все клиенты", 123456789, 1)
    async with runtime.checks.repository.sessions() as session:
        assert not (await session.scalars(select(ToolEvent))).all()


async def test_llm_failure_preserves_deterministic_report(runtime):
    llm = AsyncMock()
    llm.complete.side_effect = [
        LLMMessage(
            calls=[ToolCall("first", "get_account_overview", '{"client_id":"west_export"}')]
        ),
        RuntimeError("secret must not escape"),
    ]
    service = AgentService(runtime.checks, llm)
    answer = await service.ask("West Экспорт", 123456789, 1)
    assert "DeepSeek недоступен" in answer and "4 000,00" in answer
    assert "secret must not escape" not in answer


async def test_llm_failure_before_tools_still_checks_explicit_client(runtime):
    llm = AsyncMock()
    llm.complete.side_effect = RuntimeError("unavailable")
    answer = await AgentService(runtime.checks, llm).ask(
        "Проверь West Экспорт за 7 дней", 123456789, 1
    )
    assert "DeepSeek недоступен" in answer and "84 000,00" in answer


async def test_no_model_does_not_guess_client_or_custom_dates(runtime):
    service = AgentService(runtime.checks)
    answer = await service.ask("Почему вырос CPA?", 123456789, 1)
    assert "Укажите клиента" in answer
    answer = await service.ask("Проверь West за март", 123456789, 1)
    assert "Для этого периода" in answer


async def test_ambiguity_and_cancel(runtime):
    runtime.registry.clients["grand_line"].aliases = ["Николай"]
    assert len(runtime.registry.resolve(123456789, "Николай")) == 2
    answer = await runtime.agent.ask("Проверь Николая", 123456789, 1)
    assert "Какого клиента" in answer
    assert (123456789, 1) in runtime.agent.history
    assert (123456789, 2) not in runtime.agent.history
    runtime.agent.cancel(123456789, 1)
    assert not runtime.agent.history


def test_redaction_preserves_dates_and_decimal_values(monkeypatch):
    monkeypatch.setenv("TEST_API_KEY", "private-test-secret")
    text = "2026-09-16 13:43:39 private-test-secret a@example.org +7 (999) 123-45-67 26.66666666666666666667"
    result = redact(text)
    assert (
        "private-test-secret" not in result
        and "a@example.org" not in result
        and "999" not in result
    )
    assert "2026-09-16 13:43:39" in result and "26.66666666666666666667" in result
    assert safe_json({"spend": 89999999999, "cpa": "666.6666666666666666667"}) == {
        "spend": 89999999999,
        "cpa": "666.6666666666666666667",
    }


def test_invalid_config_isolated(settings, tmp_path):
    data = yaml.safe_load(settings.clients_config.read_text(encoding="utf-8"))
    data["clients"][0]["targets"]["target_cpa"] = -5
    data["clients"].append({"id": "bad", "unexpected": "private"})
    path = tmp_path / "clients.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    settings.clients_config = path
    registry = load_clients(settings)
    assert len(registry.clients) == 2 and len(registry.errors) == 2
    assert "private" not in str(registry.errors)


def test_duplicate_ids_fail_closed(settings, tmp_path):
    data = yaml.safe_load(settings.clients_config.read_text(encoding="utf-8"))
    data["clients"].append(data["clients"][0])
    path = tmp_path / "clients.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    settings.clients_config = path
    assert "west_export" not in load_clients(settings).clients
