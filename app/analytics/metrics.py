from calendar import monthrange
from datetime import timedelta
from decimal import Decimal

from app.analytics.periods import DateRange
from app.domain.clients import Targets
from app.domain.reports import Metrics, Totals


def ratio(numerator, denominator, scale=1) -> Decimal | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return Decimal(numerator) / Decimal(denominator) * Decimal(scale)


def calculate(totals: Totals) -> Metrics:
    return Metrics(
        **totals.model_dump(),
        ctr=ratio(totals.clicks, totals.impressions, 100),
        cpc=ratio(totals.spend, totals.clicks),
        cr=ratio(totals.conversions, totals.clicks, 100),
        cpa=ratio(totals.spend, totals.conversions),
        drr=ratio(totals.spend, totals.revenue, 100),
    )


def change(current, previous) -> dict:
    delta = None if current is None or previous is None else Decimal(current) - Decimal(previous)
    return {
        "current": current,
        "previous": previous,
        "absolute": delta,
        "percent": ratio(delta, previous, 100),
        "status": "unavailable" if delta is None else "zero_baseline" if previous == 0 else "ok",
    }


def compare(current: Metrics, previous: Metrics) -> dict:
    return {
        key: change(value, getattr(previous, key)) for key, value in current.model_dump().items()
    }


def expected_budget(period: DateRange, targets: Targets) -> Decimal | None:
    if targets.monthly_budget:
        return sum(
            (
                targets.monthly_budget / monthrange(day.year, day.month)[1]
                for day in (period.start + timedelta(days=i) for i in range(period.days))
            ),
            Decimal(0),
        )
    if targets.weekly_budget:
        return targets.weekly_budget * period.days / 7
    return None


def aggregate(rows: list[Totals]) -> Totals:
    result = {}
    for field in Totals.model_fields:
        values = [getattr(row, field) for row in rows]
        result[field] = sum(values) if values and all(v is not None for v in values) else None
    return Totals(**result)
