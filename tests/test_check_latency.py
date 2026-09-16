import asyncio
from unittest.mock import AsyncMock

from app.analytics.periods import make_period
from app.domain.reports import CheckMode, DataStatus, DirectData, Totals, TriggerSource
from app.integrations.provider import ProductionProvider


async def test_schedule_batch_leaves_manual_check_room(runtime, monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()
    original = runtime.checks.analyze
    background_started = []

    async def controlled(client, period, mode):
        if mode == CheckMode.STANDARD:
            background_started.append(client.id)
            started.set()
            await release.wait()
        return await original(client, period, mode)

    monkeypatch.setattr(runtime.checks, "analyze", controlled)
    ids = list(runtime.registry.clients)
    period = make_period("7d")
    task = asyncio.create_task(
        runtime.checks.run_check(
            ids, period, CheckMode.STANDARD, TriggerSource.SCHEDULE, chat_id=123456789
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 2)
        reports, _ = await asyncio.wait_for(
            runtime.checks.run_check(
                ids[:1], period, CheckMode.SUMMARY, TriggerSource.TELEGRAM, chat_id=123456789
            ),
            3,
        )
        assert len(reports) == 1
        assert len(background_started) == 1
    finally:
        release.set()
        await task


async def test_metrica_timeout_preserves_direct(client, monkeypatch):
    client.revenue.source = "none"
    period = make_period("14d").current
    direct = AsyncMock()
    direct.overview.return_value = DirectData(
        status=DataStatus.OK,
        period=period,
        totals=Totals(spend=100, clicks=5, impressions=100),
        campaigns=[{"Id": 1}],
    )
    metrica = AsyncMock()

    async def stall(*args):
        await asyncio.Future()

    metrica.overview.side_effect = stall
    original = asyncio.timeout
    deadlines = []

    def shortened(seconds):
        deadlines.append(seconds)
        return original(0.02)

    monkeypatch.setattr(asyncio, "timeout", shortened)
    result = await ProductionProvider(direct, metrica, AsyncMock()).snapshot(
        client, period, quick=True
    )
    assert result.direct.totals.spend == 100
    assert result.metrica.status == DataStatus.UNAVAILABLE
    assert "отведённое время" in result.metrica.limitations[0]
    assert 60 in deadlines and 45 in deadlines
