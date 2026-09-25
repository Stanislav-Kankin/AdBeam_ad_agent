import asyncio
import logging
from typing import Protocol

from app.analytics.progress import stage
from app.domain.reports import DataStatus, DirectData, MetricaData, RevenueData, Snapshot
from app.integrations.direct import DirectAdapter
from app.integrations.discovery import campaign_counters
from app.integrations.http import IntegrationError
from app.integrations.metrica import MetricaAdapter
from app.integrations.roistat import RoistatAdapter

logger = logging.getLogger(__name__)


class AnalyticsProvider(Protocol):
    mock: bool

    async def snapshot(self, client, period, *, quick=False) -> Snapshot: ...
    async def breakdown(self, client, period, dimension="campaign") -> DirectData: ...
    async def breakdown_page(self, client, period, dimension, page): ...
    async def audience_interests(self, client, period): ...
    async def metrica_direct_report(self, client, period, report_type, **kwargs): ...
    async def campaign_goals(self, client): ...
    async def goal_report(self, client, period, goal_ids, segment=None): ...
    async def demographic_adjustments(self, client, campaign_ids): ...
    async def goal_names(self, client, counter_ids): ...
    async def client_counters(self, client) -> list[int]: ...
    async def metrica_catalog(self, client, counter_ids): ...
    async def metrica_query(self, client, counter_id, start, end, **query): ...
    async def direct_query(self, client, start, end, **query): ...


def error_code(exc):
    return str(exc) if isinstance(exc, IntegrationError) else "invalid_response"


class ProductionProvider:
    mock = False

    def __init__(self, direct: DirectAdapter, metrica: MetricaAdapter, roistat: RoistatAdapter):
        self.direct, self.metrica, self.roistat = direct, metrica, roistat

    async def breakdown(self, client, period, dimension="campaign"):
        try:
            # Interactive dimensions are diagnostic top slices. Keeping at most one
            # 10k-row page prevents large accounts from exhausting bot memory. The
            # complete feed belongs in the background warehouse, not in a chat job.
            return await self.direct.breakdown(client, period, dimension, max_pages=1)
        except Exception as exc:
            return DirectData(
                status=DataStatus.UNAVAILABLE, period=period, limitations=[error_code(exc)]
            )

    async def breakdown_page(self, client, period, dimension, page):
        try:
            return await self.direct.breakdown_page(client, period, dimension, page)
        except Exception as exc:
            logger.warning(
                "Direct dimension page failed client=%s dimension=%s page=%s error=%s",
                client.id,
                dimension,
                page,
                error_code(exc),
            )
            raise

    async def campaign_goals(self, client):
        return await self.direct.campaign_goals(client)

    async def goal_report(self, client, period, goal_ids, segment=None):
        return await self.direct.goal_report(client, period, goal_ids, segment)

    async def demographic_adjustments(self, client, campaign_ids):
        return await self.direct.demographic_adjustments(client, campaign_ids)

    async def client_counters(self, client):
        """Counters a client may be queried on: selected ones and those in its campaigns.
        This is the access boundary of the universal Metrica report."""
        selected = client.metrica.selected_counter_ids()
        try:
            linked = await campaign_counters(self.direct.transport, client)
        except Exception as exc:
            logger.warning("Campaign counters failed client=%s error=%s", client.id, exc)
            linked = []
        return list(dict.fromkeys([*selected, *(int(v) for v in linked)]))

    async def metrica_catalog(self, client, counter_ids):
        return await self.metrica.catalog(client, counter_ids)

    async def metrica_query(self, client, counter_id, start, end, **query):
        return await self.metrica.query(client, counter_id, start, end, **query)

    async def direct_query(self, client, start, end, **query):
        return await self.direct.query(client, start, end, **query)

    async def goal_names(self, client, counter_ids):
        try:
            return await self.metrica.goal_names(client, counter_ids)
        except Exception as exc:
            logger.warning("Goal names failed client=%s error=%s", client.id, error_code(exc))
            return {}

    async def audience_interests(self, client, period):
        try:
            return await self.metrica.audience_interests(client, period)
        except Exception as exc:
            return {"rows": [], "limitations": [error_code(exc)], "status": "unavailable"}

    async def metrica_direct_report(self, client, period, report_type, **kwargs):
        try:
            return await self.metrica.direct_report(client, period, report_type, **kwargs)
        except Exception as exc:
            logger.warning(
                "Metrica Direct report failed client=%s report=%s error=%s",
                client.id,
                report_type,
                error_code(exc),
            )
            return {"rows": [], "limitations": [error_code(exc)], "status": "unavailable"}

    async def snapshot(self, client, period, *, quick=False):
        stage(f"{client.name}: загружаю Директ и Метрику")

        async def direct_overview():
            logger.info("Direct snapshot started client=%s period=%s", client.id, period)
            try:
                async with asyncio.timeout(60 if quick else 180):
                    result = await self.direct.overview(client, period)
            except TimeoutError:
                result = DirectData(
                    status=DataStatus.UNAVAILABLE,
                    period=period,
                    limitations=["Директ не ответил в отведённое время. Повторите запрос позже."],
                )
            logger.info("Direct snapshot finished client=%s status=%s", client.id, result.status)
            return result

        # Counter-level Metrica totals do not depend on Direct campaign IDs, so both
        # sources load concurrently; only campaign-scoped revenue waits for Direct.
        direct_task = asyncio.create_task(direct_overview())

        async def campaign_ids():
            direct = await direct_task
            # Includes inactive/historical campaigns returned by Reports and campaign metadata.
            return sorted({str(c["Id"]) for c in direct.campaigns} | {r.id for r in direct.rows})

        async def metrica():
            try:
                async with asyncio.timeout(45 if quick else 180):
                    return await self.metrica.overview(
                        client, period, None, budget=35 if quick else 165
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
                    return await self.metrica.revenue(client, period, await campaign_ids())
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

        try:
            metrica_data, revenue_data = await asyncio.gather(metrica(), bounded_revenue())
            direct = await direct_task
        finally:
            if not direct_task.done():
                direct_task.cancel()
                await asyncio.gather(direct_task, return_exceptions=True)
        logger.info("Snapshot finished client=%s metrica=%s", client.id, metrica_data.status)
        if (
            metrica_data.status == DataStatus.UNAVAILABLE
            and revenue_data.source == "metrica_ecommerce"
        ):
            revenue_data.comparable = False
        return Snapshot(direct=direct, metrica=metrica_data, revenue=revenue_data)
