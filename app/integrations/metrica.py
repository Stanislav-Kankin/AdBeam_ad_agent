import asyncio
from time import monotonic

from app.config import secret_from_env
from app.domain.reports import DataStatus, MetricaData, RevenueData
from app.integrations.discovery import campaign_counters
from app.integrations.http import IntegrationError, ReadTransport, number
from app.security import redact

BASE_URL = "https://api-metrika.yandex.net"
ATTRIBUTIONS = {
    "LC": "last",
    "FCCD": "cross_device_first",
    "LSCCD": "cross_device_last_significant",
    "AUTO": "automatic",
}


class MetricaAdapter:
    def __init__(self, transport: ReadTransport):
        self.transport = transport

    def headers(self, client):
        token = secret_from_env(client.metrica.token_env)
        if not token:
            raise IntegrationError("metrica", "missing_token")
        return {"Authorization": f"OAuth {token}"}

    async def report(self, client, period, campaign_ids, metrics):
        if (campaign_ids is not None and len(campaign_ids) > 100) or len(metrics) > 20:
            totals = [number(0) for _ in metrics]
            sampled = False
            campaign_chunks = (
                [campaign_ids[offset : offset + 100] for offset in range(0, len(campaign_ids), 100)]
                if campaign_ids is not None
                else [None]
            )
            for campaign_chunk in campaign_chunks:
                for start in range(0, len(metrics), 20):
                    data = await self.report(
                        client,
                        period,
                        campaign_chunk,
                        metrics[start : start + 20],
                    )
                    sampled |= bool(data.get("sampled"))
                    for i, value in enumerate(data["totals"]):
                        totals[start + i] += number(value)
            return {"totals": totals, "sampled": sampled}
        return await self._report(client, period, campaign_ids, metrics)

    async def _report(self, client, period, campaign_ids, metrics):
        if campaign_ids is not None and (not campaign_ids or len(campaign_ids) > 100):
            raise IntegrationError("metrica", "campaign_scope_missing_or_too_large")
        if campaign_ids is not None and any(not str(v).isdigit() for v in campaign_ids):
            raise IntegrationError("metrica", "invalid_campaign_scope")
        attribution = ATTRIBUTIONS[client.direct.attribution_model]
        params = {
            "ids": client.metrica.counter_id,
            "date1": str(period.start),
            "date2": str(period.end),
            "metrics": ",".join(metrics),
            "accuracy": "full",
            "limit": 1,
        }
        if campaign_ids is not None:
            params["filters"] = (
                f"ym:s:{attribution}DirectClickOrder=.("
                + ",".join(str(v) for v in campaign_ids)
                + ")"
            )
        data = await self.transport.json(
            "metrica",
            "GET",
            BASE_URL + "/stat/v1/data",
            headers=self.headers(client),
            params=params,
        )
        query = data.get("query", {})
        if query.get("date1") != str(period.start) or query.get("date2") != str(period.end):
            raise IntegrationError("metrica", "period_mismatch")
        if not isinstance(data.get("totals"), list) or len(data["totals"]) != len(metrics):
            raise IntegrationError("metrica", "missing_metrics")
        return data

    async def overview(self, client, period, campaign_ids, *, budget=165):
        deadline = monotonic() + budget
        if client.metrica.counter_id is not None:
            return await self._overview(client, period, campaign_ids, deadline=deadline)
        counters = await campaign_counters(self.transport, client)
        if not counters:
            return MetricaData(
                status=DataStatus.NOT_CHECKED,
                period=period,
                limitations=["В настройках кампаний не найдены счётчики Метрики."],
            )
        reports, limitations = [], []
        for counter_id in counters:
            if monotonic() >= deadline:
                limitations.append(
                    "Время загрузки Метрики исчерпано; остальные счётчики не проверены."
                )
                break
            scoped = client.model_copy(
                update={"metrica": client.metrica.model_copy(update={"counter_id": counter_id})}
            )
            try:
                report = await self._overview(
                    scoped, period, campaign_ids, all_goals=True, deadline=deadline
                )
                reports.append(report)
                limitations.extend(f"Счётчик {counter_id}: {v}" for v in report.limitations)
            except IntegrationError as exc:
                limitations.append(f"Счётчик {counter_id}: {exc}")
                if exc.code == "quota_cooldown_429":
                    limitations.append(
                        "Остальные счётчики не проверены из-за ограничения квоты Метрики."
                    )
                    break
        if not reports:
            return MetricaData(
                status=DataStatus.UNAVAILABLE, period=period, limitations=limitations
            )
        if len(reports) > 1:
            limitations.append(
                "Несколько счётчиков: визиты и цели между счётчиками не суммируются из-за возможных дублей."
            )
        return MetricaData(
            status=DataStatus.INSUFFICIENT
            if limitations or any(r.status != DataStatus.OK for r in reports)
            else DataStatus.OK,
            period=period,
            visits=reports[0].visits if len(reports) == 1 else None,
            goals=[g for r in reports for g in r.goals],
            sampled=any(r.sampled for r in reports),
            limitations=limitations,
        )

    async def _overview(self, client, period, campaign_ids, all_goals=False, deadline=None):
        path = f"/management/v1/counter/{client.metrica.counter_id}"
        info = await self.transport.json(
            "metrica", "GET", BASE_URL + path, headers=self.headers(client)
        )
        counter = info.get("counter", {})
        zone = counter.get("time_zone_name")
        if zone != "Europe/Moscow":
            raise IntegrationError("metrica", "counter_timezone_mismatch")
        data = await self.transport.json(
            "metrica", "GET", BASE_URL + path + "/goals", headers=self.headers(client)
        )
        if not isinstance(data.get("goals"), list):
            raise IntegrationError("metrica", "invalid_goals_response")
        available = {str(g["id"]): g for g in data["goals"]}
        missing = [g for g in client.metrica.main_goal_ids if g not in available]
        present = [g for g in client.metrica.main_goal_ids if g in available]
        queried = list(available) if all_goals else present
        # Commit only complete metric batches across the entire campaign scope.
        # Interrupted batches must not appear as zero or as complete totals.
        metrics = ["ym:s:visits", *[f"ym:s:goal{g}reaches" for g in queried]]
        values = [None] * len(metrics)
        sampled, limitations = False, []
        for start in range(0, len(metrics), 20):
            try:
                async with asyncio.timeout(max(0, deadline - monotonic()) if deadline else None):
                    # Goal totals describe the selected counter as a whole. Filtering every
                    # metric batch by thousands of campaign IDs multiplies requests and can
                    # exceed Metrica's report quota before one counter is complete. Direct
                    # conversions remain scoped to the account in Direct Reports.
                    report = await self.report(client, period, None, metrics[start : start + 20])
                values[start : start + 20] = [number(v) for v in report["totals"]]
                sampled |= bool(report.get("sampled"))
            except (TimeoutError, IntegrationError) as exc:
                limitations.append(
                    "Загрузка целей завершена частично: незагруженные цели не считаются нулевыми. "
                    + (str(exc) if isinstance(exc, IntegrationError) else "Истекло время загрузки.")
                )
                break
        goals = [
            {
                "id": gid,
                "name": redact(str(g.get("name", gid)))[:150],
                "primary": gid in present,
                "reaches": str(values[queried.index(gid) + 1])
                if gid in queried and values[queried.index(gid) + 1] is not None
                else None,
                "counter_id": client.metrica.counter_id,
                "type": g.get("type", ""),
            }
            for gid, g in available.items()
        ]
        return MetricaData(
            status=DataStatus.INSUFFICIENT
            if missing or sampled or limitations
            else DataStatus.OK
            if values[0]
            else DataStatus.EMPTY,
            period=period,
            visits=int(values[0]) if values[0] is not None else None,
            goals=goals,
            missing_goal_ids=missing,
            sampled=sampled,
            scope="counter",
            limitations=limitations,
            timezone=zone,
        )

    async def revenue(self, client, period, campaign_ids):
        data = await self.report(
            client, period, campaign_ids, ["ym:s:ecommerceRUBConvertedRevenue"]
        )
        return RevenueData(
            status=DataStatus.INSUFFICIENT if data.get("sampled") else DataStatus.OK,
            period=period,
            source="metrica_ecommerce",
            amount=number(data["totals"][0]),
            comparable=client.revenue.attribution_confirmed and not data.get("sampled"),
            reason="Выручка по дате визита; сопоставимость с расходом подтверждается в конфиге.",
        )
