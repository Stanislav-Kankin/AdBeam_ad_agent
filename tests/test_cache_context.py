from datetime import timedelta
from unittest.mock import AsyncMock

from app.analytics.periods import DateRange, make_period, today_moscow
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


async def test_period_is_composed_from_persisted_daily_snapshots(runtime, client):
    spy = AsyncMock(wraps=runtime.checks.provider.snapshot)
    runtime.checks.provider.snapshot = spy
    end = today_moscow() - timedelta(days=4)
    start = end - timedelta(days=2)
    daily = []
    for offset in range(3):
        day = start + timedelta(days=offset)
        daily.append(
            await runtime.checks.snapshot(client, DateRange(start=day, end=day), quick=False)
        )
    combined = await runtime.checks.snapshot(client, DateRange(start=start, end=end), quick=False)
    assert spy.await_count == 3
    assert combined.direct.totals.spend == sum(item.direct.totals.spend for item in daily)
    assert combined.metrica.visits == sum(item.metrica.visits for item in daily)
    assert combined.metrica.users is None
    assert any("не суммируются по дням" in value for value in combined.metrica.limitations)


async def test_warehouse_warm_resumes_with_next_client(runtime):
    day = today_moscow() - timedelta(days=1)
    first = await runtime.checks.warm_next(123456789, days=1)
    second = await runtime.checks.warm_next(123456789, days=1)
    assert first[1] == second[1] == day
    assert first[0] != second[0]


async def test_dimension_warehouse_resumes_with_next_dimension(runtime):
    day = today_moscow() - timedelta(days=1)
    first = await runtime.checks.warm_dimension_next(123456789, days=1)
    second = await runtime.checks.warm_dimension_next(123456789, days=1)
    assert first[1] == second[1] == day
    assert first[0] == second[0]
    assert first[2:4] == ("device", 0)
    assert second[2:4] == ("geo", 0)


async def test_dimension_warehouse_combines_daily_pages(runtime, client):
    from app.domain.reports import BreakdownRow, Totals

    end = today_moscow() - timedelta(days=1)
    start = end - timedelta(days=1)
    for day, spend in ((start, 10), (end, 15)):
        await runtime.checks.repository.save_dimension_page(
            client.id,
            day,
            "search",
            0,
            [
                BreakdownRow(
                    id="query",
                    name="купить товар",
                    totals=Totals(spend=spend, impressions=100, clicks=10, conversions=1),
                )
            ],
            last_page=True,
        )
    report = await runtime.checks.breakdown(client, DateRange(start=start, end=end), "search")
    assert report.rows[0].totals.spend == 25
    assert report.rows[0].totals.impressions == 200
    assert report.status.value == "ok"


async def test_dimension_warehouse_continues_from_saved_page(runtime, client):
    from app.domain.reports import BreakdownRow, Totals

    day = today_moscow() - timedelta(days=1)
    await runtime.checks.repository.save_dimension_page(
        client.id,
        day,
        "device",
        0,
        [BreakdownRow(id="desktop", name="DESKTOP", totals=Totals(spend=10))],
        last_page=False,
    )
    result = await runtime.checks.warm_dimension_next(123456789, days=1)
    assert result[:4] == (client.id, day, "device", 1)
    report = await runtime.checks.breakdown(client, DateRange(start=day, end=day), "device")
    assert report.rows[0].name == "DESKTOP"


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
    text, markdown = await service.explain_reports([report], "Расход: 84 000 ₽", 123456789, 9)
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


async def test_daily_digest_editor_stays_compact(runtime):
    from app.agent.service import AgentService

    llm = AsyncMock()
    llm.complete.return_value = LLMMessage(content="**Главное:** два проекта требуют внимания.")
    text, markdown = await AgentService(runtime.checks, llm).explain_daily_digest(
        "📊 Ежедневный контроль рекламы\nДва проекта требуют внимания.", 123456789
    )
    assert markdown
    assert text.startswith("**Главное:**")
    assert llm.complete.await_args.args[1] == []
    assert "до 2500 знаков" in llm.complete.await_args.args[0][0]["content"]
