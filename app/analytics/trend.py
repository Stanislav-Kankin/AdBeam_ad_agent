"""Half-year trend for a project manager: complete weeks of spend, conversions and CPA
from Direct, ad-traffic quality from Metrica, and a short computed reading of it."""

import logging
from datetime import timedelta
from decimal import Decimal

from app.analytics.metrics import aggregate, calculate, change
from app.analytics.periods import DateRange, today_moscow
from app.domain.reports import Totals

logger = logging.getLogger(__name__)
WEEKS = 26
QUALITY_METRICS = [
    "ym:s:visits",
    "ym:s:bounceRate",
    "ym:s:pageDepth",
    "ym:s:avgVisitDurationSeconds",
]


def complete_weeks(today=None, weeks=WEEKS):
    """Monday..Sunday ranges of the last complete weeks, oldest first."""
    today = today or today_moscow()
    last_sunday = today - timedelta(days=today.weekday() + 1)
    first_monday = last_sunday - timedelta(days=weeks * 7 - 1)
    return first_monday, last_sunday


async def load_trend(checks, client, today=None):
    start, end = complete_weeks(today)
    rows, limited = {}, False
    cursor = start
    while cursor <= end:
        chunk = DateRange(start=cursor, end=min(end, cursor + timedelta(days=29)))
        data = await checks.breakdown(client, chunk, "date")
        limited |= data.status.value in ("unavailable", "insufficient")
        for row in data.rows:
            rows[row.id] = row.totals
        cursor = chunk.end + timedelta(days=1)
    weeks = []
    for index in range(WEEKS):
        monday = start + timedelta(days=index * 7)
        days = [
            rows.get(str(monday + timedelta(days=d)), Totals(spend=0, clicks=0, conversions=0))
            for d in range(7)
        ]
        weeks.append({"start": monday, "totals": aggregate(days)})
    quality = await load_quality(checks, client, start, end)
    return {"start": start, "end": end, "weeks": weeks, "quality": quality, "limited": limited}


async def load_quality(checks, client, start, end):
    """Weekly bounce rate, depth and time of visits from ads; None without Metrica."""
    try:
        counters = await checks.provider.client_counters(client)
        if not counters:
            return None
        data = await checks.provider.metrica_query(
            client,
            counters[0],
            start,
            end,
            metrics=QUALITY_METRICS,
            dimensions=["ym:s:startOfWeek"],
            filters="ym:s:lastTrafficSource=='ad'",
            sort="ym:s:startOfWeek",
            limit=60,
        )
    except Exception as exc:
        logger.info("Trend quality unavailable client=%s error=%s", client.id, exc)
        return None
    by_week = {}
    for row in data.get("rows", []):
        key = row["dimensions"][0]["id"] or row["dimensions"][0]["name"]
        by_week[str(key)[:10]] = {
            "visits": row["metrics"].get("ym:s:visits"),
            "bounce_rate": row["metrics"].get("ym:s:bounceRate"),
            "page_depth": row["metrics"].get("ym:s:pageDepth"),
            "duration": row["metrics"].get("ym:s:avgVisitDurationSeconds"),
        }
    return by_week or None


def percent_text(value):
    if value is None:
        return "нет базы"
    value = Decimal(value)
    if abs(value) < 3:
        return "стабильно"
    return f"{'+' if value > 0 else '−'}{abs(value):.0f}%".replace(".", ",")


def summary(trend):
    """Plain reading of the half-year: last 4 weeks vs the 4 before, the trend from the
    first to the last two months, the best week, and ad-traffic quality."""
    weeks = trend["weeks"]
    recent = calculate(aggregate([w["totals"] for w in weeks[-4:]]))
    before = calculate(aggregate([w["totals"] for w in weeks[-8:-4]]))
    lines = [
        f"Тренд за полгода · {trend['start']:%d.%m.%Y}–{trend['end']:%d.%m.%Y}, по неделям",
        "",
        "Последние 4 недели к предыдущим 4: "
        f"расход {percent_text(change(recent.spend, before.spend)['percent'])}, "
        f"конверсии {percent_text(change(recent.conversions, before.conversions)['percent'])}, "
        f"CPA {percent_text(change(recent.cpa, before.cpa)['percent'])}.",
    ]
    key = "conversions" if any(w["totals"].conversions for w in weeks) else "spend"
    early = calculate(aggregate([w["totals"] for w in weeks[:8]]))
    late = calculate(aggregate([w["totals"] for w in weeks[-8:]]))
    shift = change(getattr(late, key), getattr(early, key))["percent"]
    label = "конверсии" if key == "conversions" else "расход"
    if shift is None:
        direction = "не с чем сравнить начало периода"
    elif shift >= 10:
        direction = f"рост: {label} {percent_text(shift)} к началу полугодия"
    elif shift <= -10:
        direction = f"спад: {label} {percent_text(shift)} к началу полугодия"
    else:
        direction = f"{label} на уровне начала полугодия"
    lines.append(f"За полгода — {direction}.")
    best = max(weeks, key=lambda w: getattr(w["totals"], key) or 0)
    if getattr(best["totals"], key):
        lines.append(f"Лучшая неделя по показателю «{label}»: с {best['start']:%d.%m}.")
    quality = trend.get("quality")
    if quality:
        ordered = [quality[k] for k in sorted(quality)]

        def mean(items, field):
            values = [Decimal(str(i[field])) for i in items if i.get(field) is not None]
            return sum(values) / len(values) if values else None

        now, was = ordered[-4:], ordered[-8:-4]
        bounce_now, bounce_was = mean(now, "bounce_rate"), mean(was, "bounce_rate")
        depth_now, depth_was = mean(now, "page_depth"), mean(was, "page_depth")
        time_now, time_was = mean(now, "duration"), mean(was, "duration")
        if bounce_now is not None:
            text = f"Качество рекламного трафика (Метрика): отказы {bounce_now:.1f}%".replace(
                ".", ","
            )
            if bounce_was is not None:
                text += f" (было {bounce_was:.1f}%)".replace(".", ",")
            if depth_now is not None:
                text += f", глубина {depth_now:.1f} стр.".replace(".", ",")
                if depth_was is not None:
                    text += f" (было {depth_was:.1f})".replace(".", ",")
            if time_now is not None:
                text += f", время на сайте {int(time_now) // 60}:{int(time_now) % 60:02d}"
                if time_was is not None:
                    text += f" (было {int(time_was) // 60}:{int(time_was) % 60:02d})"
            lines.append(text + ".")
    else:
        lines.append("Качество трафика не показано: нет доступа к счётчику Метрики.")
    if trend.get("limited"):
        lines.append("Часть недель Директ отдал не полностью.")
    lines += [
        "",
        "Если что-то настораживает, спросите бота: «почему упали конверсии в последний месяц?»",
    ]
    return "\n".join(lines)
