import asyncio
from typing import Protocol

from app.domain.reports import DataStatus, DirectData, MetricaData, RevenueData, Snapshot
from app.integrations.direct import DirectAdapter
from app.integrations.http import IntegrationError
from app.integrations.metrica import MetricaAdapter
from app.integrations.roistat import RoistatAdapter


class AnalyticsProvider(Protocol):
    mock: bool

    async def snapshot(self, client, period) -> Snapshot: ...
    async def breakdown(self, client, period, dimension="campaign") -> DirectData: ...


def error_code(exc):
    return str(exc) if isinstance(exc, IntegrationError) else "invalid_response"


class ProductionProvider:
    mock = False

    def __init__(self, direct: DirectAdapter, metrica: MetricaAdapter, roistat: RoistatAdapter):
        self.direct, self.metrica, self.roistat = direct, metrica, roistat

    async def breakdown(self, client, period, dimension="campaign"):
        try:
            return await self.direct.breakdown(client, period, dimension)
        except Exception as exc:
            return DirectData(
                status=DataStatus.UNAVAILABLE, period=period, limitations=[error_code(exc)]
            )

    async def snapshot(self, client, period):
        direct = await self.direct.overview(client, period)
        # Includes inactive/historical campaigns returned by Reports and campaign metadata.
        ids = sorted({str(c["Id"]) for c in direct.campaigns} | {r.id for r in direct.rows})

        async def metrica():
            try:
                return await self.metrica.overview(client, period, ids)
            except Exception as exc:
                return MetricaData(
                    status=DataStatus.UNAVAILABLE, period=period, limitations=[error_code(exc)]
                )

        async def revenue():
            source = client.revenue.source
            if source == "none":
                return RevenueData(
                    status=DataStatus.NOT_CHECKED,
                    period=period,
                    source=source,
                    reason="Источник выручки не настроен.",
                )
            try:
                if source == "metrica_ecommerce":
                    return await self.metrica.revenue(client, period, ids)
                return await self.roistat.revenue(client, period)
            except Exception as exc:
                return RevenueData(
                    status=DataStatus.UNAVAILABLE,
                    period=period,
                    source=source,
                    reason=error_code(exc),
                )

        metrica_data, revenue_data = await asyncio.gather(metrica(), revenue())
        if (
            metrica_data.status == DataStatus.UNAVAILABLE
            and revenue_data.source == "metrica_ecommerce"
        ):
            revenue_data.comparable = False
        return Snapshot(direct=direct, metrica=metrica_data, revenue=revenue_data)
