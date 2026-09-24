import json
from unittest.mock import AsyncMock

import pytest
import yaml
from sqlalchemy import select

from app.agent.schemas import ClientArgs
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


@pytest.mark.parametrize("period", ["30d", "60d", "60", "60 days", "2m", "2 месяца"])
def test_long_period_aliases_are_valid(period):
    args = ClientArgs(client_id="ab-grandline", period=period)
    expected = 60 if "60" in period or "2" in period else 30
    assert args.analysis_period().current.days == expected


async def test_tool_accepts_client_login_and_long_period(runtime):
    result = await runtime.agent.tools.call(
        "compare_periods",
        '{"client_id":"example-west","period":"60d"}',
        chat_id=123456789,
        request_id="long-period",
    )
    assert result["client_id"] == "west_export"
    assert result["period"]["current"]["start"] < result["period"]["current"]["end"]


async def test_agent_can_request_validated_metrica_campaign_drilldown(runtime):
    result = await runtime.agent.tools.call(
        "get_metrica_direct_report",
        json.dumps(
            {
                "client_id": "west_export",
                "period": "14d",
                "report": "campaign",
                "goal_ids": ["123456"],
                "campaign_ids": ["101"],
                "top_n": 10,
            }
        ),
        chat_id=123456789,
        request_id="metrica-campaigns",
    )

    report = result["metrica_report"]
    assert report["report"] == "campaign"
    assert report["rows"][0]["dimensions"][0]["id"] == "101"
    assert report["rows"][0]["direct"]["current"]["spend"] is not None
    assert report["goals"][0]["id"] == "123456"


async def test_metrica_drilldown_resolves_exact_campaign_name(runtime):
    client = runtime.registry.clients["west_export"]
    campaign = (await runtime.checks.provider.breakdown(client, make_period("14d").current)).rows[0]
    result = await runtime.agent.tools.call(
        "get_metrica_direct_report",
        json.dumps(
            {
                "client_id": "west_export",
                "period": "14d",
                "report": "search_phrase",
                "campaign_ids": [campaign.name],
            }
        ),
        chat_id=123456789,
        request_id="campaign-name",
    )

    assert result["metrica_report"]["rows"][0]["dimensions"][0]["id"] == campaign.id


async def test_metrica_drilldown_returns_resolution_details(runtime):
    result = await runtime.agent.tools.call(
        "get_metrica_direct_report",
        json.dumps(
            {
                "client_id": "west_export",
                "period": "14d",
                "report": "search_phrase",
                "campaign_ids": ["campaign that does not exist"],
            }
        ),
        chat_id=123456789,
        request_id="campaign-missing",
    )

    assert result["status"] == "invalid"
    assert result["unresolved"] == ["campaign that does not exist"]


def test_custom_iso_period_is_valid_json():
    args = ClientArgs.model_validate_json(
        '{"client_id":"ab-grandline","start_date":"2026-07-23",'
        '"end_date":"2026-09-20","compare_start":"2026-05-24",'
        '"compare_end":"2026-07-22"}'
    )
    assert args.analysis_period().current.days == 60


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
    assert "DeepSeek недоступен" in answer and "84 000 ₽" in answer


async def test_llm_failure_understands_two_month_period(runtime):
    llm = AsyncMock()
    llm.complete.side_effect = RuntimeError("unavailable")
    run_check = AsyncMock(wraps=runtime.checks.run_check)
    runtime.checks.run_check = run_check
    await AgentService(runtime.checks, llm).ask(
        "Сделай анализ Grand Line за последние 2 месяца", 123456789, 1
    )
    assert run_check.await_args.args[1].current.days == 60


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
    assert (await runtime.checks.repository.conversation(123456789, 1))["messages"]
    assert not (await runtime.checks.repository.conversation(123456789, 2))["messages"]
    await runtime.agent.cancel(123456789, 1)
    assert not (await runtime.checks.repository.conversation(123456789, 1))["messages"]


def test_redaction_preserves_dates_and_decimal_values(monkeypatch):
    monkeypatch.setenv("TEST_API_KEY", "private-test-secret")
    from app.security import refresh_secrets

    refresh_secrets()
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
