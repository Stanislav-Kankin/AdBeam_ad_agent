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
    return f"{'+' if value > 0 else '−'}{abs(value):.0f}%"


def num(value, digits=1):
    return f"{Decimal(value):.{digits}f}".replace(".", ",")


def minutes(seconds):
    return f"{int(seconds) // 60}:{int(seconds) % 60:02d}"


def mean(items, field):
    values = [Decimal(str(i[field])) for i in items if i.get(field) is not None]
    return sum(values) / len(values) if values else None


def quality_periods(trend):
    """Ad-traffic quality of the last 4 weeks and the 4 before (None without Metrica)."""
    quality = trend.get("quality")
    if not quality:
        return None
    ordered = [quality[k] for k in sorted(quality)]
    now, was = ordered[-4:], ordered[-8:-4]
    return {
        field: (mean(now, field), mean(was, field))
        for field in ("bounce_rate", "page_depth", "duration")
    }


def summary(trend):
    """Manager-readable half-year: last 4 weeks vs the 4 before, the trend from the
    first to the last two months, the best week, ad-traffic quality and a plain
    reading. Markdown: bold labels and bullets."""
    weeks = trend["weeks"]
    recent = calculate(aggregate([w["totals"] for w in weeks[-4:]]))
    before = calculate(aggregate([w["totals"] for w in weeks[-8:-4]]))
    spend_shift = change(recent.spend, before.spend)["percent"]
    conv_shift = change(recent.conversions, before.conversions)["percent"]
    cpa_shift = change(recent.cpa, before.cpa)["percent"]
    lines = [
        f"📊 **Тренд за полгода** · {trend['start']:%d.%m}–{trend['end']:%d.%m.%Y}, полные недели",
        "",
        "**Последние 4 недели к предыдущим 4**",
        f"• Расход: **{percent_text(spend_shift)}**",
        f"• Конверсии: **{percent_text(conv_shift)}**",
        f"• CPA: **{percent_text(cpa_shift)}**"
        + (
            " — дешевле"
            if cpa_shift is not None and cpa_shift <= -3
            else " — дороже"
            if cpa_shift is not None and cpa_shift >= 3
            else ""
        ),
    ]
    key = "conversions" if any(w["totals"].conversions for w in weeks) else "spend"
    label = "конверсии" if key == "conversions" else "расход"
    early = calculate(aggregate([w["totals"] for w in weeks[:8]]))
    late = calculate(aggregate([w["totals"] for w in weeks[-8:]]))
    shift = change(getattr(late, key), getattr(early, key))["percent"]
    if shift is None:
        direction = "сравнить с началом периода не с чем"
    elif shift >= 10:
        direction = f"**рост**: {label} {percent_text(shift)} к началу полугодия"
    elif shift <= -10:
        direction = f"**спад**: {label} {percent_text(shift)} к началу полугодия"
    else:
        direction = f"{label} **на уровне** начала полугодия"
    lines += ["", "**За полгода**", f"• {direction[0].upper() + direction[1:]}"]
    best = max(weeks, key=lambda w: getattr(w["totals"], key) or 0)
    best_value = getattr(best["totals"], key)
    if best_value:
        amount = f"{int(best_value)}" if key == "conversions" else f"{int(best_value):,} ₽"
        lines.append(
            f"• Лучшая неделя: с **{best['start']:%d.%m}** — {amount.replace(',', ' ')}"
            + (" конверсий" if key == "conversions" else "")
        )

    quality = quality_periods(trend)
    notes = []
    if quality and quality["bounce_rate"][0] is not None:
        lines += ["", "**Качество рекламного трафика** · Метрика, 4 недели к предыдущим 4"]
        bounce_now, bounce_was = quality["bounce_rate"]
        text = f"• Отказы: **{num(bounce_now)}%**"
        if bounce_was is not None:
            delta = bounce_now - bounce_was
            text += f" (было {num(bounce_was)}%)"
            if abs(delta) >= 1:
                text += f" — {'выше' if delta > 0 else 'ниже'} на {num(abs(delta))} п.п."
                if delta >= 2:
                    notes.append("больше отказов")
        lines.append(text)
        for field, title, render, unit in (
            ("page_depth", "Глубина", num, " стр."),
            ("duration", "Время на сайте", minutes, ""),
        ):
            now, was = quality[field]
            if now is None:
                continue
            text = f"• {title}: **{render(now)}{unit}**"
            if was:
                delta = (now - was) / was * 100
                text += f" (было {render(was)}{unit})"
                if abs(delta) >= 5:
                    text += f" — {'больше' if delta > 0 else 'меньше'} на {abs(delta):.0f}%"
                    if delta <= -10:
                        notes.append(
                            "меньше " + ("страниц" if field == "page_depth" else "времени")
                        )
            lines.append(text)
    else:
        lines += [
            "",
            "Качество трафика не показано: доступ к Метрике закрыт клиентом или недоступен.",
        ]

    reading = []
    if conv_shift is not None and cpa_shift is not None:
        if conv_shift >= 3 and cpa_shift <= -3:
            reading.append("Конверсий больше, и они дешевле — тренд хороший.")
        elif conv_shift <= -3 and cpa_shift >= 3:
            reading.append("Конверсий меньше, и они дороже — стоит разобрать с техспецом.")
        elif conv_shift <= -3:
            reading.append("Конверсии снижаются — спросите бота, какие кампании дали спад.")
    if notes:
        reading.append(
            "Посетители из рекламы вовлечены слабее ("
            + ", ".join(notes)
            + "): уточните у техспеца новые площадки, аудитории или посадочные."
        )
    if reading:
        lines += ["", "**Что это значит**", *[f"• {item}" for item in reading]]
    if trend.get("limited"):
        lines.append("Часть недель Директ отдал не полностью.")
    lines += ["", "Спросите бота, если что-то настораживает: «почему выросли отказы в сентябре?»"]
    return "\n".join(lines)
