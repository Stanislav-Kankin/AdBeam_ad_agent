from datetime import date, timedelta
from decimal import Decimal

from app.analytics.metrics import aggregate
from app.analytics.periods import today_moscow
from app.domain.reports import (
    BreakdownRow,
    DataStatus,
    DirectData,
    MetricaData,
    RevenueData,
    Snapshot,
    Totals,
)


class MockProvider:
    """Stable daily fixtures; overlapping ranges always contain the same observations."""

    mock = True

    def __init__(self, anchor: date | None = None):
        self.anchor = anchor or today_moscow()

    def daily_rows(self, client, day):
        recent = day >= self.anchor - timedelta(days=7)
        scenario = client.mock_scenario
        # Search remains stable; network deterioration drives the red case.
        specs = [
            ("101", "Поиск — основные товары", 3000, 10000, 150, 3),
            ("102", "РСЯ — каталог", 2000, 20000, 100, 2),
        ]
        if recent and scenario == "red":
            specs[1] = ("102", "РСЯ — каталог", 9000, 18000, 100, 0)
        elif recent and scenario == "yellow":
            specs[1] = ("102", "РСЯ — каталог", 4000, 20000, 100, 2)
        rows = []
        for id_, name, spend, impressions, clicks, conversions in specs:
            rows.append(
                BreakdownRow(
                    id=id_,
                    name=name,
                    totals=Totals(
                        spend=spend,
                        impressions=impressions,
                        clicks=clicks,
                        conversions=conversions,
                        revenue=conversions * 15000,
                    ),
                )
            )
        return rows

    async def breakdown(self, client, period, dimension="campaign"):
        if client.mock_scenario == "unavailable":
            return DirectData(
                status=DataStatus.UNAVAILABLE,
                period=period,
                limitations=["MOCK: источник недоступен."],
            )
        daily = [
            self.daily_rows(client, period.start + timedelta(days=i)) for i in range(period.days)
        ]
        rows = [
            BreakdownRow(
                id=daily[0][i].id,
                name=daily[0][i].name,
                totals=aggregate([day[i].totals for day in daily]),
            )
            for i in range(2)
        ]
        if dimension == "date":
            return DirectData(
                status=DataStatus.OK,
                period=period,
                rows=[
                    BreakdownRow(
                        id=str(period.start + timedelta(days=index)),
                        name=str(period.start + timedelta(days=index)),
                        totals=aggregate([row.totals for row in values]),
                    )
                    for index, values in enumerate(daily)
                ],
            )
        if dimension != "campaign":
            names = {
                "device": ["DESKTOP", "MOBILE"],
                "geo": ["Москва", "Санкт-Петербург"],
                "search": ["купить товар оптом", "каталог товаров"],
                "placement": ["example.org", "example.net"],
                "age": ["AGE_25_34", "AGE_35_44"],
                "gender": ["GENDER_FEMALE", "GENDER_MALE"],
                "income": ["HIGH", "OTHER"],
            }[dimension]
            rows = [
                row.model_copy(update={"id": f"{dimension}_{i}", "name": names[i]})
                for i, row in enumerate(rows)
            ]
        return DirectData(
            status=DataStatus.OK,
            period=period,
            totals=aggregate([r.totals for r in rows]),
            rows=rows,
            campaigns=[
                {
                    "Id": 101,
                    "Name": "Поиск",
                    "State": "ON",
                    "Status": "ACCEPTED",
                    "StatusPayment": "ALLOWED",
                    "Currency": "RUB",
                },
                {
                    "Id": 102,
                    "Name": "РСЯ",
                    "State": "ON",
                    "Status": "ACCEPTED",
                    "StatusPayment": "ALLOWED",
                    "Currency": "RUB",
                },
            ],
            campaigns_status=DataStatus.OK,
        )

    async def breakdown_page(self, client, period, dimension, page):
        if page:
            return [], True
        report = await self.breakdown(client, period, dimension)
        return report.rows, True

    async def audience_interests(self, client, period):
        return {
            "status": "ok",
            "scope": "site_counter",
            "rows": [
                {"name": "Строительство и ремонт", "visits": 1200, "users": 900, "affinity": 185},
                {"name": "Загородная недвижимость", "visits": 800, "users": 650, "affinity": 160},
            ],
            "limitations": [],
        }

    async def metrica_direct_report(
        self, client, period, report_type, *, goal_ids=None, campaign_ids=None, limit=20
    ):
        goals = list(goal_ids or client.metrica.main_goal_ids)
        rows = []
        for row in (await self.breakdown(client, period, "campaign")).rows[:limit]:
            if campaign_ids and row.id not in campaign_ids:
                continue
            metrics = {
                "visits": row.totals.clicks,
                "users": max(0, (row.totals.clicks or 0) - 5),
                "bounce_rate": Decimal("18.5"),
                "page_depth": Decimal("3.2"),
                "avg_visit_duration_seconds": Decimal("145"),
            }
            for goal_id in goals:
                metrics[f"goal_{goal_id}_visits"] = row.totals.conversions
                metrics[f"goal_{goal_id}_conversion_rate"] = (
                    Decimal(row.totals.conversions or 0) / Decimal(row.totals.clicks or 1) * 100
                )
            rows.append(
                {
                    "key": row.id,
                    "dimensions": [{"id": row.id, "name": row.name}],
                    "metrics": metrics,
                }
            )
        return {
            "status": "ok",
            "counter_id": client.metrica.counter_id or 1,
            "report": report_type,
            "attribution": "lastsign",
            "goals": [{"id": value, "name": "Основная цель"} for value in goals],
            "rows": rows,
            "total_rows": len(rows),
            "truncated": False,
            "sampled": False,
            "sample_share": 1,
            "limitations": [],
        }

    async def snapshot(self, client, period, *, quick=False):
        direct = await self.breakdown(client, period)
        missing = client.mock_scenario == "unavailable"
        goals = [
            {
                "id": goal,
                "name": "Основная цель",
                "primary": True,
                "reaches": str(
                    (direct.totals.conversions or Decimal(0))
                    / max(1, len(client.metrica.main_goal_ids))
                ),
            }
            for goal in client.metrica.main_goal_ids
        ]
        metrica = MetricaData(
            status=DataStatus.UNAVAILABLE if missing else DataStatus.OK,
            period=period,
            visits=None if missing else direct.totals.clicks,
            goals=goals,
        )
        source = client.revenue.source
        revenue = RevenueData(
            status=DataStatus.NOT_CHECKED
            if source == "none"
            else DataStatus.UNAVAILABLE
            if missing
            else DataStatus.OK,
            period=period,
            source=source,
            amount=direct.totals.revenue if source != "none" else None,
            comparable=source != "none",
            reason="Источник выручки не настроен."
            if source == "none"
            else "MOCK: синтетическая выручка.",
        )
        return Snapshot(direct=direct, metrica=metrica, revenue=revenue)
