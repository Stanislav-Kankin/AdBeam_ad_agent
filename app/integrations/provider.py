import asyncio
import logging
from typing import Protocol

from app.analytics.progress import stage
from app.domain.reports import DataStatus, DirectData, MetricaData, RevenueData, Snapshot
from app.integrations.direct import DirectAdapter
from app.integrations.http import IntegrationError
from app.integrations.metrica import MetricaAdapter
from app.integrations.roistat import RoistatAdapter

logger = logging.getLogger(__name__)


class AnalyticsProvider(Protocol):
    mock: bool

    async def snapshot(self, client, period, *, quick=False) -> Snapshot: ...
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

    async def snapshot(self, client, period, *, quick=False):
        stage(f"{client.name}: ожидаю отчёт Директа")
        logger.info("Direct snapshot started client=%s period=%s", client.id, period)
        try:
            async with asyncio.timeout(60 if quick else 180):
                direct = await self.direct.overview(client, period)
        except TimeoutError:
            direct = DirectData(
                status=DataStatus.UNAVAILABLE,
                period=period,
                limitations=["Директ не ответил в отведённое время. Повторите запрос позже."],
            )
        logger.info("Direct snapshot finished client=%s status=%s", client.id, direct.status)
        # Includes inactive/historical campaigns returned by Reports and campaign metadata.
        ids = sorted({str(c["Id"]) for c in direct.campaigns} | {r.id for r in direct.rows})

        async def metrica():
            try:
                stage(f"{client.name}: загружаю Метрику и цели")
                async with asyncio.timeout(45 if quick else 180):
                    return await self.metrica.overview(
                        client, period, ids, budget=35 if quick else 165
                    )
            except TimeoutError:
                logger.warning("Metrica deadline exceeded client=%s quick=%s", client.id, quick)
                return MetricaData(
                    status=DataStatus.UNAVAILABLE,
                    period=period,
                    limitations=[
                        "Метрика не завершила загрузку в отведённое время; показаны доступные данные Директа. Повторите подробную проверку позже."
                    ],
                )
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

        async def bounded_revenue():
            try:
                async with asyncio.timeout(45 if quick else 180):
                    return await revenue()
            except TimeoutError:
                return RevenueData(
                    status=DataStatus.UNAVAILABLE,
                    period=period,
                    source=client.revenue.source,
                    reason="Истекло время загрузки выручки.",
                )

        metrica_data, revenue_data = await asyncio.gather(metrica(), bounded_revenue())
        logger.info("Snapshot finished client=%s metrica=%s", client.id, metrica_data.status)
        if (
            metrica_data.status == DataStatus.UNAVAILABLE
            and revenue_data.source == "metrica_ecommerce"
        ):
            revenue_data.comparable = False
        return Snapshot(direct=direct, metrica=metrica_data, revenue=revenue_data)
