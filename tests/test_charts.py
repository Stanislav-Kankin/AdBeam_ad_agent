from io import BytesIO
from unittest.mock import AsyncMock

from PIL import Image

from app.analytics.periods import make_period
from app.reporting.charts import _chart_series, render_dynamics


async def test_dynamics_chart_is_readable_png(runtime, client):
    period = make_period("14d")
    current, previous = await runtime.checks.dynamics(client, period)
    payload = render_dynamics(client.name, period, current, previous)
    assert payload.startswith(b"\x89PNG\r\n\x1a\n")
    with Image.open(BytesIO(payload)) as image:
        assert image.size == (1600, 1120)
        assert image.mode == "RGB"


async def test_date_breakdown_has_one_row_per_day(runtime, client):
    period = make_period("30d").current
    result = await runtime.checks.provider.breakdown(client, period, "date")
    assert len(result.rows) == 30
    assert result.rows[0].id == str(period.start)
    assert result.rows[-1].id == str(period.end)


async def test_90_day_dynamics_uses_bounded_date_chunks(runtime, client):
    original = runtime.checks.provider.breakdown
    runtime.checks.provider.breakdown = AsyncMock(wraps=original)

    current, previous = await runtime.checks.dynamics(client, make_period("90d"))

    calls = runtime.checks.provider.breakdown.await_args_list
    assert len(calls) == 6
    assert all(call.args[1].days <= 30 and call.args[2] == "date" for call in calls)
    assert len(current.rows) == 90
    assert len(previous.rows) == 90
    rows, dates, grouping = _chart_series(current)
    assert len(rows) == len(dates) == 13
    assert grouping == "неделям"
    assert sum(row.clicks or 0 for row in rows) == current.totals.clicks
