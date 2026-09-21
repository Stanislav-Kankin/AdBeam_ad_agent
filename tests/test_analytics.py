from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.analytics.diagnostics import snapshot_metrics
from app.analytics.metrics import aggregate, calculate, change, expected_budget
from app.analytics.periods import MOSCOW, AnalysisPeriod, DateRange, make_period
from app.analytics.rules import evaluate, tracking_health
from app.domain.clients import Targets
from app.domain.reports import CheckMode, DataStatus, Totals, TriggerSource


def test_periods_and_moscow():
    p = make_period("7d", date(2026, 1, 1))
    assert p.current.start == date(2025, 12, 25)
    assert p.current.end == date(2025, 12, 31)
    assert p.previous.end == date(2025, 12, 24)
    p = make_period("yesterday", date(2026, 9, 16))
    assert p.previous.start == date(2026, 9, 8)
    assert datetime.fromisoformat("2026-09-15T22:00:00+00:00").astimezone(MOSCOW).date() == date(
        2026, 9, 16
    )


@pytest.mark.parametrize("value", ["0d", "91d", "100d", "-1d", "today", "7", "1w"])
def test_reject_invalid_periods(value):
    with pytest.raises(ValueError):
        make_period(value)


def test_comparison_rejects_overlap_unequal_or_incomplete():
    a = DateRange(start=date(2026, 1, 1), end=date(2026, 1, 7))
    with pytest.raises(ValidationError):
        AnalysisPeriod(current=a, previous=a)
    with pytest.raises(ValidationError):
        AnalysisPeriod(
            current=a, previous=DateRange(start=date(2025, 12, 31), end=date(2025, 12, 31))
        )
    with pytest.raises(ValueError):
        a.completed(date(2026, 1, 7))


def test_metrics_are_ratios_of_totals_and_decimal():
    totals = aggregate(
        [
            Totals(spend="100.10", impressions=1000, clicks=10, conversions=1, revenue=200),
            Totals(spend="199.90", impressions=9000, clicks=90, conversions=2, revenue=800),
        ]
    )
    m = calculate(totals)
    assert (m.spend, m.ctr, m.cpc, m.cr, m.cpa, m.drr) == (Decimal(300), 1, 3, 3, 100, 30)
    assert change(20, 0)["percent"] is None
    assert change(20, 0)["status"] == "zero_baseline"
    assert change(None, 10)["status"] == "unavailable"


def test_zero_and_missing_denominators():
    m = calculate(Totals(spend=100, clicks=0, impressions=0, conversions=0, revenue=0))
    assert all(getattr(m, k) is None for k in ("ctr", "cpc", "cr", "cpa", "drr"))
    assert aggregate([Totals(spend=10), Totals()]).spend is None


def test_budget_across_months_and_leap_year():
    period = DateRange(start=date(2024, 2, 29), end=date(2024, 3, 1))
    targets = Targets(monthly_budget=8990)
    assert expected_budget(period, targets) == Decimal(8990) / 29 + Decimal(8990) / 31
    assert expected_budget(period, Targets(weekly_budget=7000)) == 2000


async def test_mock_scenarios_and_standard_checks(runtime):
    reports, text = await runtime.checks.run_check(
        list(runtime.registry.clients),
        make_period(),
        CheckMode.STANDARD,
        TriggerSource.INTERNAL,
        chat_id=123456789,
    )
    assert {r.client_id: r.level for r in reports} == {
        "west_export": "red",
        "grand_line": "yellow",
        "fresh_parfum": "green",
    }
    assert "MOCK" in text
    west = reports[0]
    assert west.drivers[0]["name"] == "РСЯ — каталог"
    assert west.drivers[0]["spend_delta"] == Decimal(49000)
    assert any(s.type == "campaign_without_conversions" for s in west.signals)
    assert west.checks["устройства"] == "ok"
    assert reports[1].current.drr is None


async def test_mock_overlapping_periods_consistent(runtime, client):
    p = make_period()
    left = DateRange(start=p.current.start, end=p.current.start)
    rest = DateRange(start=p.current.start + timedelta(days=1), end=p.current.end)
    full = await runtime.checks.provider.snapshot(client, p.current)
    a = await runtime.checks.provider.snapshot(client, left)
    b = await runtime.checks.provider.snapshot(client, rest)
    assert full.direct.totals.spend == a.direct.totals.spend + b.direct.totals.spend


async def test_tracking_failure_suppresses_conversion_claims(runtime, client):
    p = make_period()
    a, b = await runtime.checks.snapshots(client, p)
    a.metrica.status = DataStatus.UNAVAILABLE
    health = tracking_health(client, a, b, p)
    metrics = snapshot_metrics(a, healthy=health["healthy"])
    assert metrics.cpa is None and metrics.cr is None
    assert metrics.spend == 84000
    signals = evaluate(client, metrics, snapshot_metrics(b), p, health, a)
    assert any(s.type == "tracking" for s in signals)
    assert not any(s.type in ("cpa_high", "spend_without_conversions", "cr_drop") for s in signals)


async def test_tracking_failure_hides_campaign_conversion_metrics(runtime, client, monkeypatch):
    period = make_period()
    current, previous = await runtime.checks.snapshots(client, period)
    current.metrica.status = DataStatus.UNAVAILABLE

    async def snapshots(*args, **kwargs):
        return current, previous

    monkeypatch.setattr(runtime.checks, "snapshots", snapshots)
    report = await runtime.checks.analyze(client, period, CheckMode.STANDARD)
    assert report.drivers
    assert all(
        row[key][metric] is None
        for row in report.drivers
        for key in ("current", "previous")
        for metric in ("conversions", "cr", "cpa")
    )


async def test_zero_conversions_everywhere_is_tracking_signal(runtime, client):
    p = make_period()
    a, b = await runtime.checks.snapshots(client, p)
    a.direct.totals.conversions = Decimal(0)
    for g in a.metrica.goals:
        g["reaches"] = "0"
    health = tracking_health(client, a, b, p)
    assert not health["healthy"]
    assert "исчезли" in " ".join(health["reasons"])


async def test_conversion_delay_and_low_volume_suppress_alerts(runtime, client):
    p = make_period()
    a, b = await runtime.checks.snapshots(client, p)
    client.targets.conversion_delay_days = 3
    report = await runtime.checks.analyze(client, p, CheckMode.STANDARD)
    assert not any(
        s.type in ("cpa_high", "cr_drop", "campaign_without_conversions") for s in report.signals
    )
    assert any("дополняться" in line for line in report.limitations)
    client.targets.conversion_delay_days = 0
    client.targets.minimum_clicks = 100000
    report = await runtime.checks.analyze(client, p, CheckMode.STANDARD)
    assert not any(s.type == "cpa_high" for s in report.signals)


async def test_no_data_never_green(runtime, client):
    client.mock_scenario = "unavailable"
    report = await runtime.checks.analyze(client, make_period(), CheckMode.STANDARD)
    assert report.level != "green"
    assert report.current.spend is None
    assert report.current.cpa is None


async def test_partial_client_failure_keeps_other_reports(runtime, monkeypatch):
    original = runtime.checks.provider.snapshot

    async def failing(client, period, *, quick=False):
        if client.id == "west_export":
            raise RuntimeError("not sent to output")
        return await original(client, period, quick=quick)

    monkeypatch.setattr(runtime.checks.provider, "snapshot", failing)
    reports, text = await runtime.checks.run_check(
        list(runtime.registry.clients),
        make_period(),
        CheckMode.STANDARD,
        TriggerSource.INTERNAL,
        chat_id=123456789,
    )
    assert len(reports) == 2
    assert "West Экспорт" in text and "west_export" not in text and "not sent" not in text


async def test_summary_does_not_request_dimensions(runtime, monkeypatch):
    original = runtime.checks.provider.breakdown

    async def limited(client, period, dimension="campaign"):
        assert dimension == "campaign"
        return await original(client, period, dimension)

    monkeypatch.setattr(runtime.checks.provider, "breakdown", limited)
    report = await runtime.checks.analyze(
        runtime.registry.clients["fresh_parfum"], make_period(), CheckMode.SUMMARY
    )
    assert report.checks["устройства"] == "not_checked"


async def test_standard_with_signals_does_not_request_heavy_dimensions(runtime, monkeypatch):
    requested = []
    original = runtime.checks.provider.breakdown

    async def recording(client, period, dimension="campaign"):
        requested.append(dimension)
        return await original(client, period, dimension)

    monkeypatch.setattr(runtime.checks.provider, "breakdown", recording)
    await runtime.checks.analyze(
        runtime.registry.clients["west_export"], make_period(), CheckMode.STANDARD
    )
    assert "device" in requested
    assert not {"geo", "search", "placement"}.intersection(requested)
