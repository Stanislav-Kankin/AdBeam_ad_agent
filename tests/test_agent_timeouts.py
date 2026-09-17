from unittest.mock import AsyncMock

from app.agent.service import AgentService
from app.analytics.periods import make_period
from app.integrations.deepseek import LLMMessage, ToolCall
from app.integrations.metrica import MetricaAdapter


async def test_tool_timeout_never_starts_second_check(runtime):
    llm = AsyncMock()
    llm.complete.return_value = LLMMessage(calls=[ToolCall("1", "get_account_overview", "{}")])
    service = AgentService(runtime.checks, llm)
    service.tools.call = AsyncMock(side_effect=TimeoutError)
    runtime.checks.run_check = AsyncMock()
    answer = await service.ask("Обзор West за вчера", 123456789, 1)
    runtime.checks.run_check.assert_not_called()
    assert "DeepSeek недоступен" not in answer
    assert "Повторная проверка автоматически не запускается" in answer


async def test_ping_does_not_call_model(runtime):
    llm = AsyncMock()
    answer = await AgentService(runtime.checks, llm).ask("проверка связи\\", 123456789, 1)
    assert "На связи" in answer
    llm.complete.assert_not_called()


async def test_partial_goal_batches_preserve_completed_totals(client, monkeypatch):
    monkeypatch.setenv(client.metrica.token_env, "synthetic")
    transport = AsyncMock()
    transport.json.side_effect = [
        {"counter": {"time_zone_name": "Europe/Moscow"}},
        {"goals": [{"id": i, "name": str(i)} for i in range(25)]},
    ]
    adapter = MetricaAdapter(transport)
    adapter.report = AsyncMock(
        side_effect=[{"totals": [100, 90, 200, 20, 2, 60] + [7] * 14}, TimeoutError()]
    )
    result = await adapter._overview(client, make_period().current, ["1"], all_goals=True)
    assert result.status == "insufficient"
    assert result.visits == 100
    assert sum(g["reaches"] == "7" for g in result.goals) == 14
    assert sum(g["reaches"] is None for g in result.goals) == 11
    assert result.limitations
