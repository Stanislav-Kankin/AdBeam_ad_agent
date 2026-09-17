from unittest.mock import AsyncMock

from app.analytics.periods import make_period
from app.domain.reports import CheckMode
from app.integrations.deepseek import LLMMessage


async def test_snapshots_are_reused_for_same_client_and_period(runtime, client):
    spy = AsyncMock(wraps=runtime.checks.provider.snapshot)
    runtime.checks.provider.snapshot = spy
    period = make_period("7d")
    first = await runtime.checks.snapshots(client, period, CheckMode.STANDARD)
    second = await runtime.checks.snapshots(client, period, CheckMode.STANDARD)
    assert spy.await_count == 2
    assert first[0] == second[0]
    assert first[1] == second[1]


async def test_full_snapshot_satisfies_quick_request(runtime, client):
    spy = AsyncMock(wraps=runtime.checks.provider.snapshot)
    runtime.checks.provider.snapshot = spy
    period = make_period("7d")
    await runtime.checks.snapshots(client, period, CheckMode.STANDARD)
    await runtime.checks.snapshots(client, period, CheckMode.SUMMARY)
    assert spy.await_count == 2


async def test_conversation_context_survives_new_agent_service(runtime):
    from app.agent.service import AgentService

    period = make_period("7d")
    await runtime.checks.repository.save_conversation(
        123456789,
        7,
        [
            {"role": "user", "content": "Проверь West"},
            {"role": "assistant", "content": "Проверил."},
        ],
        active_client_id="west_export",
        period=period.model_dump(mode="json"),
    )
    service = AgentService(runtime.checks, llm=None)
    answer = await service.ask("А что у него с расходом?", 123456789, 7)
    assert "Укажите клиента" not in answer
    assert "Детерминированная стандартная проверка" in answer


async def test_model_edits_ready_report_without_tools(runtime, client):
    from app.agent.service import AgentService

    report = await runtime.checks.analyze(client, make_period("7d"), CheckMode.STANDARD)
    llm = AsyncMock()
    llm.complete.return_value = LLMMessage(content="**Расход:** 84 000 ₽\nСледующий шаг.")
    service = AgentService(runtime.checks, llm)
    text, markdown = await service.explain_reports(
        [report], "Расход: 84 000 ₽", 123456789, 9
    )
    assert markdown
    assert text.startswith("**Расход:**")
    assert llm.complete.await_args.args[1] == []
    context = await runtime.checks.repository.conversation(123456789, 9)
    assert context["active_client_id"] == client.id


async def test_model_editor_failure_returns_deterministic_report(runtime, client):
    from app.agent.service import AgentService

    report = await runtime.checks.analyze(client, make_period("7d"), CheckMode.STANDARD)
    llm = AsyncMock()
    llm.complete.side_effect = TimeoutError
    text, markdown = await AgentService(runtime.checks, llm).explain_reports(
        [report], "Надёжный отчёт", 123456789, 9
    )
    assert text == "Надёжный отчёт"
    assert not markdown
