import hashlib
import json
from decimal import Decimal

from app.analytics.periods import make_period
from app.domain.reports import CheckMode, TriggerSource
from app.reporting.formatter import audience_report, campaigns_view, card, compact, detailed


async def test_single_client_report_separates_decisions_from_diagnostics(runtime, client):
    report = await runtime.checks.analyze(client, make_period("7d"), CheckMode.STANDARD)
    report.limitations.extend(
        [
            "HTTP 429 retry_exhausted for /stat/v1/data",
            "В ответе источника отсутствует часть строк.",
        ]
    )

    main = card(report)
    campaigns = campaigns_view(report)
    technical = detailed(report)

    assert "**Показатели** · изменение · сейчас / было" in main
    assert "Данные:" in main
    assert "Требует внимания" in main or "Рисков не найдено" in main
    assert "HTTP 429" not in main
    assert "HTTP 429" in technical
    assert ".00" not in main and ",00 ₽" not in main
    assert len(main) < 2000
    assert "Кампании" in campaigns and "CPA" in campaigns


async def test_card_shows_delta_before_values_and_hides_noise(runtime, client):
    report = await runtime.checks.analyze(client, make_period("7d"), CheckMode.STANDARD)
    report.changes["clicks"]["percent"] = 2
    main = card(report)
    clicks = next(line for line in main.splitlines() if line.startswith("Клики:"))
    assert clicks.startswith("Клики: **стабильно**")
    spend = next(line for line in main.splitlines() if line.startswith("Расход:"))
    assert spend.index("**") < spend.index("₽")


async def test_card_groups_small_campaigns_without_conversions(runtime, client):
    from app.domain.reports import Signal

    report = await runtime.checks.analyze(client, make_period("7d"), CheckMode.STANDARD)
    report.current.spend = Decimal(2_168_241)

    def signal(type_, level, message, **actual):
        return Signal(
            type=type_,
            level=level,
            message=message,
            actual=actual,
            period=report.period,
            evidence="",
            confidence="high",
            sufficient_data=True,
            next_check="",
        )

    report.signals = [
        signal(
            "campaign_without_conversions",
            "yellow",
            f"Кампания «C{i}» расходует без основных конверсий.",
            name=f"C{i}",
            spend=spend,
        )
        for i, spend in enumerate((12400, 9800, 7600, 5200, 4300), 1)
    ] + [signal("cpa_change", "yellow", "CPA вырос на 11,5% относительно прошлого периода.")]
    report.drivers = []
    text = card(report)
    risks = text.split("Требует внимания**")[1].split("\n\n")[0].strip().splitlines()
    assert risks[0] == "1. CPA вырос на 11,5% относительно прошлого периода."
    assert risks[1] == (
        "2. Без основных конверсий 5 кампаний на 39 300 ₽ (1,8% расхода): "
        "«C1» 12 400 ₽, «C2» 9 800 ₽, «C3» 7 600 ₽ и ещё 2."
    )


async def test_small_campaign_without_conversions_is_not_critical(runtime):
    client = runtime.registry.clients["west_export"]
    report = await runtime.checks.analyze(client, make_period("7d"), CheckMode.STANDARD)
    waste = [s for s in report.signals if s.type == "campaign_without_conversions"]
    assert waste
    for s in waste:
        assert s.level == ("red" if s.actual["share_percent"] >= 10 else "yellow")


async def test_card_names_expensive_campaign(runtime, client):
    report = await runtime.checks.analyze(client, make_period("7d"), CheckMode.STANDARD)
    report.current.cpa, report.current.spend = 1000, 100000
    report.drivers = [
        {
            "id": "1",
            "name": "Кровля (поиск)",
            "spend_delta": 25840,
            "current": {"spend": 28180, "cpa": 14090, "conversions": 2},
            "previous": {"spend": 2340, "cpa": 2340, "conversions": 1},
        }
    ]
    assert "«Кровля (поиск)»: CPA 14 090 ₽" in card(report)


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


async def test_failed_audience_is_not_cached_and_says_so(runtime, client, monkeypatch):
    from unittest.mock import AsyncMock

    from app.domain.reports import DataStatus, DirectData

    period = make_period("14d")

    async def broken(_client, date_range, dimension="campaign"):
        return DirectData(
            status=DataStatus.UNAVAILABLE,
            period=date_range,
            limitations=["direct: report_pending_timeout"],
        )

    spy = AsyncMock(side_effect=broken)
    monkeypatch.setattr(runtime.checks.provider, "breakdown", spy)
    first = await runtime.checks.audience(client, period)
    await runtime.checks.audience(client, period)
    text = audience_report(client.name, first)

    assert spy.await_count == 6  # three slices, retried: the failure was not cached
    assert "не загрузилось из Директа" in text
    assert "• нет данных" not in text


async def test_metrica_campaign_report_compares_periods_and_is_cached(runtime, client, monkeypatch):
    from unittest.mock import AsyncMock

    period = make_period("14d")
    spy = AsyncMock(wraps=runtime.checks.provider.metrica_direct_report)
    monkeypatch.setattr(runtime.checks.provider, "metrica_direct_report", spy)

    first = await runtime.checks.metrica_report(
        client, period, "campaign", goal_ids=["123456"], top_n=10
    )
    second = await runtime.checks.metrica_report(
        client, period, "campaign", goal_ids=["123456"], top_n=10
    )

    assert first == second
    assert spy.await_count == 2  # current and previous; the repeated request uses cache
    assert first["rows"][0]["dimensions"][0]["name"]
    assert "visits" in first["rows"][0]["changes"]
    assert "goal_123456_visits" in first["rows"][0]["current"]


async def test_metrica_report_ignores_legacy_cache_after_schema_change(
    runtime, client, monkeypatch
):
    from unittest.mock import AsyncMock

    period = make_period("14d")
    old_options = {
        "report": "campaign",
        "goals": sorted(client.metrica.main_goal_ids),
        "campaigns": [],
        "top_n": 20,
    }
    old_kind = (
        "mr"
        + hashlib.sha256(json.dumps(old_options, sort_keys=True).encode("utf-8")).hexdigest()[:8]
    )
    await runtime.checks.repository.save_analysis(
        client.id,
        period.current,
        old_kind,
        {"status": "unavailable", "rows": [], "limitations": ["legacy"]},
    )
    spy = AsyncMock(wraps=runtime.checks.provider.metrica_direct_report)
    monkeypatch.setattr(runtime.checks.provider, "metrica_direct_report", spy)

    report = await runtime.checks.metrica_report(client, period, "campaign")

    assert spy.await_count == 2
    assert report["rows"]
    assert "legacy" not in report["limitations"]


async def test_metrica_campaign_report_sorts_numbers_and_survives_stopped_campaigns(
    runtime, client, monkeypatch
):
    # Barka case: a campaign present only in the previous period has neither current
    # spend nor visits; mixing its 0 key with string amounts raised TypeError.
    from unittest.mock import AsyncMock

    from app.domain.reports import BreakdownRow, Totals

    period = make_period("7d")
    current, previous = await runtime.checks.snapshots(client, period)
    current.direct.rows = [
        BreakdownRow(id="1", name="Малая", totals=Totals(spend=9000, clicks=10)),
        BreakdownRow(id="2", name="Большая", totals=Totals(spend=10000, clicks=10)),
    ]
    previous.direct.rows = [
        *current.direct.rows,
        BreakdownRow(id="3", name="Остановлена", totals=Totals(spend=500, clicks=5)),
    ]

    async def snapshots(*args, **kwargs):
        return current, previous

    async def empty(_client, date_range, report_type, **kwargs):
        return {"status": "ok", "rows": [], "limitations": [], "total_rows": 0}

    monkeypatch.setattr(runtime.checks, "snapshots", snapshots)
    monkeypatch.setattr(
        runtime.checks.provider, "metrica_direct_report", AsyncMock(side_effect=empty)
    )
    report = await runtime.checks.metrica_report(client, period, "campaign")
    names = [row["dimensions"][0]["name"] for row in report["rows"]]
    assert names == ["Большая", "Малая", "Остановлена"]


async def test_metrica_campaign_report_keeps_direct_campaign_without_visits(
    runtime, client, monkeypatch
):
    from unittest.mock import AsyncMock

    async def only_search(_client, date_range, report_type, **kwargs):
        return {
            "status": "ok",
            "counter_id": 12345678,
            "report": report_type,
            "attribution": "last",
            "goals": [],
            "rows": [
                {
                    "key": "101",
                    "dimensions": [{"id": "101", "name": "Поиск"}],
                    "metrics": {"visits": 10},
                }
            ],
            "total_rows": 1,
            "truncated": False,
            "sampled": False,
            "limitations": [],
        }

    monkeypatch.setattr(
        runtime.checks.provider, "metrica_direct_report", AsyncMock(side_effect=only_search)
    )
    report = await runtime.checks.metrica_report(client, make_period("7d"), "campaign")
    by_id = {row["dimensions"][0]["id"]: row for row in report["rows"]}

    assert set(by_id) == {"101", "102"}
    assert by_id["102"]["current"] == {}
    assert by_id["102"]["direct"]["current"]["spend"] is not None
