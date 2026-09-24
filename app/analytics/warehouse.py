from collections import defaultdict
from decimal import Decimal

from app.analytics.metrics import aggregate
from app.domain.reports import (
    BreakdownRow,
    DataStatus,
    DirectData,
    MetricaData,
    RevenueData,
    Snapshot,
)


def combined_status(values):
    values = list(values)
    if values and all(value == values[0] for value in values):
        return values[0]
    # A day without impressions is complete data, not a gap: the period is OK.
    if values and set(values) <= {DataStatus.OK, DataStatus.EMPTY}:
        return DataStatus.OK
    return DataStatus.INSUFFICIENT


def unique(items):
    return list(dict.fromkeys(item for item in items if item))


def weighted(snapshots, field):
    pairs = [
        (getattr(item.metrica, field), item.metrica.visits)
        for item in snapshots
        if getattr(item.metrica, field) is not None and item.metrica.visits is not None
    ]
    if len(pairs) != len(snapshots) or not sum(visits for _, visits in pairs):
        return None
    return sum(Decimal(str(value)) * visits for value, visits in pairs) / sum(
        visits for _, visits in pairs
    )


def combine_daily(snapshots: list[Snapshot], period):
    if not snapshots:
        raise ValueError("Daily snapshots are required")

    direct_rows = {}
    row_totals = defaultdict(list)
    campaigns = {}
    for snapshot in snapshots:
        for row in snapshot.direct.rows:
            direct_rows[row.id] = row.name
            row_totals[row.id].append(row.totals)
        for campaign in snapshot.direct.campaigns:
            if "Id" in campaign:
                campaigns[str(campaign["Id"])] = campaign
    direct = DirectData(
        status=combined_status(item.direct.status for item in snapshots),
        period=period,
        # Empty days carry no totals at all; summing them would null the whole period.
        totals=aggregate(
            [item.direct.totals for item in snapshots if item.direct.status != DataStatus.EMPTY]
        ),
        rows=[
            BreakdownRow(id=id_, name=direct_rows[id_], totals=aggregate(totals))
            for id_, totals in row_totals.items()
        ],
        campaigns=list(campaigns.values()),
        campaigns_status=combined_status(item.direct.campaigns_status for item in snapshots),
        currency=snapshots[-1].direct.currency,
        limitations=unique(
            limitation for item in snapshots for limitation in item.direct.limitations
        ),
    )

    goals, goal_counts = {}, defaultdict(int)
    for snapshot in snapshots:
        for goal in snapshot.metrica.goals:
            key = (str(goal.get("counter_id", "")), str(goal.get("id", "")))
            goal_counts[key] += 1
            stored = goals.setdefault(key, {**goal, "reaches": Decimal(0)})
            reaches = goal.get("reaches")
            if stored["reaches"] is not None:
                stored["reaches"] = (
                    stored["reaches"] + Decimal(str(reaches)) if reaches is not None else None
                )
    for key, goal in goals.items():
        if goal_counts[key] != len(snapshots):
            goal["reaches"] = None
        elif goal["reaches"] is not None:
            goal["reaches"] = str(goal["reaches"])

    def sum_field(field):
        values = [getattr(item.metrica, field) for item in snapshots]
        return sum(values) if all(value is not None for value in values) else None

    metrica_limitations = unique(
        limitation for item in snapshots for limitation in item.metrica.limitations
    )
    if len(snapshots) > 1:
        metrica_limitations.append(
            "Уникальные посетители не суммируются по дням; показатель users для сборного периода не рассчитан."
        )
    metrica = MetricaData(
        status=combined_status(item.metrica.status for item in snapshots),
        period=period,
        visits=sum_field("visits"),
        users=snapshots[0].metrica.users if len(snapshots) == 1 else None,
        pageviews=sum_field("pageviews"),
        bounce_rate=weighted(snapshots, "bounce_rate"),
        page_depth=weighted(snapshots, "page_depth"),
        avg_visit_duration_seconds=weighted(snapshots, "avg_visit_duration_seconds"),
        goals=list(goals.values()),
        missing_goal_ids=unique(
            goal for item in snapshots for goal in item.metrica.missing_goal_ids
        ),
        sampled=any(item.metrica.sampled for item in snapshots),
        scope=snapshots[-1].metrica.scope,
        timezone=snapshots[-1].metrica.timezone,
        limitations=metrica_limitations,
    )

    revenues = [item.revenue for item in snapshots]
    revenue_amounts = [item.amount for item in revenues]
    revenue_ok = all(item.status == DataStatus.OK for item in revenues)
    revenue = RevenueData(
        status=DataStatus.OK if revenue_ok else combined_status(item.status for item in revenues),
        period=period,
        source=revenues[-1].source,
        amount=sum(revenue_amounts, Decimal(0))
        if revenue_ok and all(value is not None for value in revenue_amounts)
        else None,
        comparable=revenue_ok and all(item.comparable for item in revenues),
        reason="; ".join(unique(item.reason for item in revenues)),
    )
    return Snapshot(direct=direct, metrica=metrica, revenue=revenue)
