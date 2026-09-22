from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from app.analytics.metrics import aggregate, calculate, change
from app.domain.reports import DirectData, Totals

WIDTH, HEIGHT = 1600, 1120
BLUE = "#2563EB"
BLUE_FILL = "#DBEAFE"
INK = "#172033"
MUTED = "#65728A"
GRID = "#E5EAF2"
CARD = "#F7F9FC"
GREEN = "#12805C"
RED = "#C2414B"


def _font(size, *, bold=False):
    names = (
        "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
    )
    for name in names:
        if Path(name).exists() or not Path(name).is_absolute():
            try:
                return ImageFont.truetype(name, size)
            except OSError:
                pass
    return ImageFont.load_default(size=size)


def _short(value, *, money=False):
    if value is None:
        return "нет данных"
    value = Decimal(value)
    absolute = abs(value)
    if absolute >= 1_000_000:
        text = f"{value / 1_000_000:.1f} млн"
    elif absolute >= 1_000:
        text = f"{value / 1_000:.1f} тыс."
    else:
        text = f"{value:.2f}" if money else f"{value:.0f}"
    return text.replace(".", ",") + (" ₽" if money else "")


def _delta(current, previous, *, lower_is_better=None):
    percent = change(current, previous)["percent"]
    if percent is None:
        return "—", MUTED
    sign = "+" if percent > 0 else ""
    if not percent:
        color = MUTED
    elif lower_is_better is None:
        color = BLUE
    else:
        improved = percent < 0 if lower_is_better else percent > 0
        color = GREEN if improved else RED
    return f"{sign}{percent:.1f}%".replace(".", ","), color


def _daily(data: DirectData):
    by_day = {row.id: row.totals for row in data.rows}
    result = []
    for offset in range(data.period.days):
        day = data.period.start + timedelta(days=offset)
        result.append(
            by_day.get(
                str(day),
                Totals(spend=0, impressions=0, clicks=0, conversions=0),
            )
        )
    return result


def _chart_series(data: DirectData):
    rows = _daily(data)
    if data.period.days <= 30:
        return (
            rows,
            [data.period.start + timedelta(days=index) for index in range(len(rows))],
            "дням",
        )
    weekly = [aggregate(rows[index : index + 7]) for index in range(0, len(rows), 7)]
    dates = [data.period.start + timedelta(days=index) for index in range(0, len(rows), 7)]
    return weekly, dates, "неделям"


def _dashed(draw, points, fill, width=3, segment=12):
    for left, right in zip(points, points[1:], strict=False):
        x1, y1 = left
        x2, y2 = right
        distance = max(abs(x2 - x1), abs(y2 - y1))
        steps = max(1, int(distance / segment))
        for index in range(0, steps, 2):
            a = index / steps
            b = min(1, (index + 1) / steps)
            draw.line(
                (
                    x1 + (x2 - x1) * a,
                    y1 + (y2 - y1) * a,
                    x1 + (x2 - x1) * b,
                    y1 + (y2 - y1) * b,
                ),
                fill=fill,
                width=width,
            )


def _panel(draw, box, title, current, previous, dates, *, money=False, x_label="Дни"):
    x1, y1, x2, y2 = box
    draw.rounded_rectangle(box, radius=26, fill="white", outline=GRID, width=2)
    draw.text((x1 + 28, y1 + 22), title, font=_font(25, bold=True), fill=INK)
    values = [float(v or 0) for v in [*current, *previous]]
    high = max(values, default=0) or 1
    plot = (x1 + 75, y1 + 72, x2 - 28, y2 - 72)
    px1, py1, px2, py2 = plot
    for index in range(4):
        y = py1 + (py2 - py1) * index / 3
        draw.line((px1, y, px2, y), fill=GRID, width=2)
        label = _short(Decimal(str(high * (3 - index) / 3)), money=money)
        draw.text((x1 + 18, y - 10), label, font=_font(16), fill=MUTED)

    def points(series):
        count = max(1, len(series) - 1)
        return [
            (
                px1 + (px2 - px1) * index / count,
                py2 - (py2 - py1) * float(value or 0) / high,
            )
            for index, value in enumerate(series)
        ]

    new_points = points(current)
    if len(new_points) > 1:
        area = [(new_points[0][0], py2), *new_points, (new_points[-1][0], py2)]
        draw.polygon(area, fill=BLUE_FILL)
    old_points = points(previous)
    if len(old_points) > 1:
        _dashed(draw, old_points, "#8492A8", 3)
    if len(new_points) > 1:
        draw.line(new_points, fill=BLUE, width=4, joint="curve")
    for x, y in new_points[:: max(1, len(new_points) // 12)]:
        draw.ellipse((x - 4, y - 4, x + 4, y + 4), fill=BLUE)
    if dates:
        count = len(dates)
        tick_indexes = sorted({0, (count - 1) // 3, 2 * (count - 1) // 3, count - 1})
        for index in tick_indexes:
            x = px1 if count == 1 else px1 + (px2 - px1) * index / (count - 1)
            draw.line((x, py2, x, py2 + 6), fill=MUTED, width=2)
            draw.text(
                (x, py2 + 9),
                dates[index].strftime("%d.%m"),
                font=_font(14),
                fill=MUTED,
                anchor="ma",
            )
        draw.text(
            ((px1 + px2) / 2, py2 + 34),
            f"{x_label} текущего периода",
            font=_font(13),
            fill=MUTED,
            anchor="ma",
        )


def render_dynamics(client_name, period, current: DirectData, previous: DirectData) -> bytes:
    image = Image.new("RGB", (WIDTH, HEIGHT), "#F1F5F9")
    draw = ImageDraw.Draw(image)
    draw.text((65, 45), client_name, font=_font(42, bold=True), fill=INK)
    draw.text(
        (65, 100),
        f"Динамика · {period.current.label()} против {period.previous.label()}",
        font=_font(24),
        fill=MUTED,
    )
    now_rows, dates, grouping = _chart_series(current)
    old_rows, _, _ = _chart_series(previous)
    now_total, old_total = aggregate(now_rows), aggregate(old_rows)
    now_metrics, old_metrics = calculate(now_total), calculate(old_total)
    cards = (
        ("Расход", now_metrics.spend, old_metrics.spend, True, None),
        ("Клики", now_metrics.clicks, old_metrics.clicks, False, False),
        ("Конверсии", now_metrics.conversions, old_metrics.conversions, False, False),
        ("CPA", now_metrics.cpa, old_metrics.cpa, True, True),
    )
    card_width = 350
    for index, (label, current_value, previous_value, money, lower_is_better) in enumerate(cards):
        x = 65 + index * (card_width + 24)
        draw.rounded_rectangle((x, 155, x + card_width, 290), radius=24, fill=CARD)
        draw.text((x + 24, 178), label, font=_font(20), fill=MUTED)
        draw.text(
            (x + 24, 215),
            _short(current_value, money=money),
            font=_font(31, bold=True),
            fill=INK,
        )
        delta, color = _delta(current_value, previous_value, lower_is_better=lower_is_better)
        draw.text((x + 235, 227), delta, font=_font(20, bold=True), fill=color)

    _panel(
        draw,
        (65, 330, 1535, 650),
        f"Расход по {grouping}, ₽",
        [row.spend for row in now_rows],
        [row.spend for row in old_rows],
        dates,
        money=True,
        x_label="Недели" if grouping == "неделям" else "Дни",
    )
    _panel(
        draw,
        (65, 680, 790, 1010),
        f"Клики по {grouping}, шт.",
        [row.clicks for row in now_rows],
        [row.clicks for row in old_rows],
        dates,
        x_label="Недели" if grouping == "неделям" else "Дни",
    )
    _panel(
        draw,
        (815, 680, 1535, 1010),
        f"Конверсии по {grouping}, шт.",
        [row.conversions for row in now_rows],
        [row.conversions for row in old_rows],
        dates,
        x_label="Недели" if grouping == "неделям" else "Дни",
    )
    draw.line((65, 1055, 105, 1055), fill=BLUE, width=4)
    draw.text((120, 1040), "Текущий период", font=_font(19), fill=INK)
    _dashed(draw, [(330, 1055), (370, 1055)], "#9AA6BA", 3, 8)
    draw.text((385, 1040), "Предыдущий период", font=_font(19), fill=MUTED)
    draw.text((1535, 1040), "AdBeam", font=_font(19, bold=True), fill=MUTED, anchor="ra")
    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()
