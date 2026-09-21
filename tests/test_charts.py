from io import BytesIO

from PIL import Image

from app.analytics.periods import make_period
from app.reporting.charts import render_dynamics


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
