from app.analytics.periods import make_period
from app.domain.reports import CheckMode, TriggerSource
from app.reporting.formatter import audience_report, brief, compact, detailed, executive


async def test_single_client_report_separates_decisions_from_diagnostics(runtime, client):
    report = await runtime.checks.analyze(client, make_period("7d"), CheckMode.STANDARD)
    report.limitations.extend(
        [
            "HTTP 429 retry_exhausted for /stat/v1/data",
            "В ответе источника отсутствует часть строк.",
        ]
    )

    main = executive(report)
    technical = detailed(report)

    assert "Основное изменение:" in main
    assert "Динамика показателей:" in main
    assert "Что сделать:" in main
    assert "Полнота данных:" in main
    assert "HTTP 429" not in main
    assert "HTTP 429" in technical

    overview = brief(report)
    assert len(overview.split("\n\n")) == 2
    assert len(overview) < 1000


async def test_portfolio_report_is_ranked_and_bounded(runtime):
    reports, text = await runtime.checks.run_check(
        list(runtime.registry.clients),
        make_period("7d"),
        CheckMode.STANDARD,
        TriggerSource.INTERNAL,
        chat_id=123456789,
    )
    rendered = compact(reports, reports[0].period)

    assert "Главные проекты:" in rendered
    assert "Что сделать:" in rendered
    assert "Показы:" not in rendered
    assert "Для технической расшифровки" in rendered
    assert len(rendered) < len(text) + 1500


async def test_latest_run_can_feed_technical_details(runtime, client):
    before = await runtime.checks.repository.latest_run(123456789, 77)
    assert before is None

    reports, _ = await runtime.checks.run_check(
        [client.id],
        make_period("7d"),
        CheckMode.STANDARD,
        TriggerSource.AGENT,
        chat_id=123456789,
        user_id=77,
    )
    latest = await runtime.checks.repository.latest_run(123456789, 77)

    assert latest is not None
    assert latest["status"] == "completed"
    assert latest["reports"][0]["client_id"] == reports[0].client_id


async def test_audience_breakdown_is_readable_and_cached(runtime, client, monkeypatch):
    from unittest.mock import AsyncMock

    period = make_period("14d")
    spy = AsyncMock(wraps=runtime.checks.provider.breakdown)
    monkeypatch.setattr(runtime.checks.provider, "breakdown", spy)

    first = await runtime.checks.audience(client, period)
    second = await runtime.checks.audience(client, period)
    text = audience_report(client.name, first)

    assert first == second
    assert spy.await_count == 3
    assert "Возраст:" in text and "Пол:" in text and "Доход:" in text
    assert "Долгосрочные интересы" in text
    assert "аффинити" in text
