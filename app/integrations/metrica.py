from app.config import secret_from_env
from app.domain.reports import DataStatus, MetricaData, RevenueData
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
        if not campaign_ids or len(campaign_ids) > 100:
            raise IntegrationError("metrica", "campaign_scope_missing_or_too_large")
        if any(not str(v).isdigit() for v in campaign_ids):
            raise IntegrationError("metrica", "invalid_campaign_scope")
        attribution = ATTRIBUTIONS[client.direct.attribution_model]
        params = {
            "ids": client.metrica.counter_id,
            "date1": str(period.start),
            "date2": str(period.end),
            "metrics": ",".join(metrics),
            "accuracy": "full",
            "limit": 1,
            "filters": f"ym:s:{attribution}DirectClickOrder=.("
            + ",".join(str(v) for v in campaign_ids)
            + ")",
        }
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

    async def overview(self, client, period, campaign_ids):
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
        report = await self.report(
            client,
            period,
            campaign_ids,
            ["ym:s:visits", *[f"ym:s:goal{g}reaches" for g in present]],
        )
        values = [number(v) for v in report["totals"]]
        goals = [
            {
                "id": gid,
                "name": redact(str(g.get("name", gid)))[:150],
                "primary": gid in present,
                "reaches": str(values[present.index(gid) + 1]) if gid in present else None,
            }
            for gid, g in available.items()
        ]
        return MetricaData(
            status=DataStatus.INSUFFICIENT
            if missing or report.get("sampled")
            else DataStatus.OK
            if values[0]
            else DataStatus.EMPTY,
            period=period,
            visits=int(values[0]),
            goals=goals,
            missing_goal_ids=missing,
            sampled=bool(report.get("sampled")),
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
