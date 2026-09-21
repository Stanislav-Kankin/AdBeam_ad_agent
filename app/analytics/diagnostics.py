import asyncio
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import monotonic

from app.analytics.metrics import aggregate, calculate, change, compare
from app.analytics.periods import DateRange, today_moscow
from app.analytics.progress import stage
from app.analytics.rules import evaluate, tracking_health
from app.analytics.warehouse import combine_daily
from app.domain.reports import (
    BreakdownRow,
    CheckMode,
    ClientReport,
    DataStatus,
    DirectData,
    Metrics,
    Signal,
    Snapshot,
    Totals,
    TriggerSource,
)
from app.reporting.formatter import compact, detailed

logger = logging.getLogger(__name__)
WAREHOUSE_DIMENSIONS = ("device", "geo", "search", "placement")


def snapshot_metrics(snapshot, *, healthy=True):
    totals = snapshot.direct.totals.model_copy(deep=True)
    if snapshot.direct.status != DataStatus.OK:
        totals = Totals()
    if not healthy or snapshot.metrica.status != DataStatus.OK or snapshot.metrica.missing_goal_ids:
        totals.conversions = None
    revenue = snapshot.revenue
    totals.revenue = revenue.amount if revenue.status == DataStatus.OK else None
    metrics = calculate(totals)
    if not revenue.comparable or revenue.period != snapshot.direct.period:
        metrics.drr = None
    return metrics


def drivers(current, previous):
    if current.status != DataStatus.OK or previous.status != DataStatus.OK:
        return []
    now, before = {r.id: r for r in current.rows}, {r.id: r for r in previous.rows}
    result = []
    zero = Totals(spend=0, clicks=0, impressions=0, conversions=0)
    for id_ in now.keys() | before.keys():
        a, b = now.get(id_), before.get(id_)
        cur, prev = a.totals if a else zero, b.totals if b else zero
        result.append(
            {
                "id": id_,
                "name": (a or b).name,
                "spend_delta": change(cur.spend, prev.spend)["absolute"],
                "conversions_delta": change(cur.conversions, prev.conversions)["absolute"],
                "current": calculate(cur).model_dump(mode="json"),
                "previous": calculate(prev).model_dump(mode="json"),
            }
        )
    return sorted(result, key=lambda r: abs(r["spend_delta"] or 0), reverse=True)


class CheckService:
    def __init__(self, registry, provider, repository):
        self.registry, self.provider, self.repository = registry, provider, repository
        self.semaphore = asyncio.Semaphore(3)
        self.schedule_semaphore = asyncio.Semaphore(1)

    async def snapshots(self, client, period, mode=CheckMode.STANDARD):
        return await asyncio.gather(
            self.snapshot(client, period.current, quick=mode == CheckMode.SUMMARY),
            self.snapshot(client, period.previous, quick=mode == CheckMode.SUMMARY),
        )

    async def snapshot(self, client, period, *, quick=False):
        cached = await self.repository.cached_snapshot(client.id, period, quick=quick)
        if cached is not None:
            logger.info(
                "Snapshot cache hit client=%s period=%s..%s quick=%s",
                client.id,
                period.start,
                period.end,
                quick,
            )
            result = Snapshot.model_validate(cached)
            await self.repository.save_daily_snapshot(client.id, period, result, quick=quick)
            return result
        daily = await self.repository.daily_snapshots(client.id, period, quick=quick)
        if daily is not None:
            logger.info(
                "Daily warehouse hit client=%s period=%s..%s days=%s",
                client.id,
                period.start,
                period.end,
                period.days,
            )
            result = combine_daily([Snapshot.model_validate(value) for value in daily], period)
            await self.repository.save_snapshot(
                client.id, period, result, quick=quick, ttl_minutes=240
            )
            return result
        result = await self.provider.snapshot(client, period, quick=quick)
        complete = result.direct.status == DataStatus.OK and result.metrica.status == DataStatus.OK
        age_days = (today_moscow() - period.end).days
        ttl = (
            10 if not complete else 1440 if age_days > client.targets.conversion_delay_days else 240
        )
        await self.repository.save_snapshot(client.id, period, result, quick=quick, ttl_minutes=ttl)
        await self.repository.save_daily_snapshot(client.id, period, result, quick=quick)
        return result

    async def warm_next(self, chat_id, days=30):
        clients = self.registry.visible(chat_id)
        if not clients:
            return None
        yesterday = today_moscow() - timedelta(days=1)
        oldest = yesterday - timedelta(days=days - 1)
        existing = await self.repository.fresh_daily_keys(
            [client.id for client in clients], oldest, yesterday
        )
        async with self.schedule_semaphore:
            for offset in range(days):
                day = yesterday - timedelta(days=offset)
                for client in clients:
                    if (client.id, str(day)) in existing:
                        continue
                    logger.info("Warehouse warm client=%s day=%s", client.id, day)
                    await self.snapshot(client, DateRange(start=day, end=day), quick=False)
                    return client.id, day
        return None

    async def warm_dimension_next(self, chat_id, days=30):
        clients = self.registry.visible(chat_id)
        if not clients:
            return None
        yesterday = today_moscow() - timedelta(days=1)
        oldest = yesterday - timedelta(days=days - 1)
        progress = await self.repository.dimension_progress(
            [client.id for client in clients], oldest, yesterday, WAREHOUSE_DIMENSIONS
        )
        async with self.schedule_semaphore:
            for offset in range(days):
                day = yesterday - timedelta(days=offset)
                period = DateRange(start=day, end=day)
                for client in clients:
                    for dimension in WAREHOUSE_DIMENSIONS:
                        item = progress.get((client.id, str(day), dimension))
                        if item:
                            pages = item["pages"]
                            last = item["last"]
                            if last is not None and pages == set(range(last + 1)):
                                continue
                            page = next(
                                (value for value in range(max(pages) + 2) if value not in pages),
                                0,
                            )
                        else:
                            page = 0
                        if page >= 100:
                            continue
                        logger.info(
                            "Dimension warehouse warm client=%s day=%s dimension=%s page=%s",
                            client.id,
                            day,
                            dimension,
                            page + 1,
                        )
                        rows, complete = await self.provider.breakdown_page(
                            client, period, dimension, page
                        )
                        await self.repository.save_dimension_page(
                            client.id,
                            day,
                            dimension,
                            page,
                            rows,
                            last_page=complete,
                        )
                        return client.id, day, dimension, page, complete
        return None

    async def breakdown(self, client, period, dimension):
        stored = await self.repository.dimension(client.id, period, dimension)
        if stored is not None:
            logger.info(
                "Dimension warehouse hit client=%s period=%s..%s dimension=%s",
                client.id,
                period.start,
                period.end,
                dimension,
            )
            by_id = {}
            for value in stored["rows"]:
                row = BreakdownRow.model_validate(value)
                item = by_id.setdefault(row.id, {"name": row.name, "totals": []})
                item["totals"].append(row.totals)
            rows = [
                BreakdownRow(id=id_, name=value["name"], totals=aggregate(value["totals"]))
                for id_, value in by_id.items()
            ]
            return DirectData(
                status=DataStatus.OK if rows else DataStatus.EMPTY,
                period=period,
                rows=rows,
                totals=aggregate([row.totals for row in rows]),
                limitations=[
                    "Все страницы разреза сохранены; для интерактивного анализа взяты "
                    "500 строк с наибольшим расходом за каждый день."
                ]
                if stored["truncated"]
                else [],
            )
        return await self.provider.breakdown(client, period, dimension)

    async def analyze(self, client, period, mode):
        logger.info("Check queued client=%s mode=%s", client.id, mode)
        async with self.semaphore:
            logger.info("Check started client=%s mode=%s", client.id, mode)
            stage(f"{client.name}: получаю данные Яндекса")
            current, previous = await self.snapshots(client, period, mode)
            health = tracking_health(client, current, previous, period)
            a, b = snapshot_metrics(current, healthy=health["healthy"]), snapshot_metrics(previous)
            signals = evaluate(client, a, b, period, health, current)
            checks = {
                "доступность": "ok" if health["healthy"] else "insufficient",
                "основные цели": current.metrica.status.value,
                "статусы кампаний": current.direct.campaigns_status.value,
                "кампании": "not_checked",
                "устройства": "not_checked",
                "география": "not_checked",
                "запросы": "not_checked",
                "площадки": "not_checked",
                "бюджет": "ok"
                if client.targets.monthly_budget or client.targets.weekly_budget
                else "not_checked",
            }
            limitations = [
                *current.direct.limitations,
                *current.metrica.limitations,
                *previous.direct.limitations,
                *previous.metrica.limitations,
            ]
            if not health["healthy"]:
                limitations.append("CPA и CR не рассчитаны: доступность аналитики не подтверждена.")
            mature = (
                today_moscow() - period.current.end
            ).days > client.targets.conversion_delay_days
            if not mature:
                limitations.append(
                    f"Конверсии могут дополняться {client.targets.conversion_delay_days} дн.; сигналы CPA/CR и расхода без конверсий подавлены."
                )
            if (
                current.revenue.status != DataStatus.OK
                or not current.revenue.comparable
                or a.drr is None
            ):
                limitations.append(
                    "ДРР не рассчитан: "
                    + (
                        current.revenue.reason
                        or "выручка отсутствует, равна нулю или несопоставима."
                    )
                )
            if previous.direct.status != DataStatus.OK or previous.metrica.status != DataStatus.OK:
                limitations.append(
                    "Предыдущий период неполный или недоступен; сравнение ограничено."
                )
            if (a.clicks or 0) < client.targets.minimum_clicks:
                limitations.append("Недостаточный объём кликов для выводов об эффективности.")
            campaign_drivers = []
            if mode != CheckMode.SUMMARY:
                checks["кампании"] = current.direct.status.value
                campaign_drivers = drivers(current.direct, previous.direct)[:10]
                if not health["healthy"]:
                    for row in campaign_drivers:
                        row["conversions_delta"] = None
                threshold = client.targets.minimum_spend_for_analysis
                if client.targets.target_cpa:
                    threshold = min(
                        threshold,
                        client.targets.target_cpa
                        * Decimal(str(client.targets.no_conversion_cpa_multiple)),
                    )
                if mature and health["healthy"]:
                    for row in current.direct.rows:
                        if (
                            row.totals.conversions == 0
                            and (row.totals.spend or 0) >= threshold
                            and (row.totals.clicks or 0) >= client.targets.minimum_clicks
                        ):
                            signals.append(
                                Signal(
                                    type="campaign_without_conversions",
                                    level="red",
                                    message=f"Кампания «{row.name}» расходует без основных конверсий.",
                                    actual={
                                        "campaign_id": row.id,
                                        "spend": row.totals.spend,
                                        "conversions": 0,
                                    },
                                    period=period,
                                    evidence="Порог расхода и кликов превышен; аналитика доступна.",
                                    confidence="high",
                                    sufficient_data=True,
                                    next_check="Проверить целевой трафик этой кампании и посадочную страницу.",
                                )
                            )
                for dim, label in [
                    ("device", "устройства"),
                    *(
                        [("geo", "география"), ("search", "запросы"), ("placement", "площадки")]
                        if mode == CheckMode.DEEP
                        else []
                    ),
                ]:
                    stage(f"{client.name}: анализирую разрез «{label}»")
                    try:
                        async with asyncio.timeout(90):
                            cur, prev = await asyncio.gather(
                                self.breakdown(client, period.current, dim),
                                self.breakdown(client, period.previous, dim),
                            )
                    except TimeoutError:
                        cur = DirectData(
                            status=DataStatus.UNAVAILABLE,
                            period=period.current,
                            limitations=[
                                f"Разрез «{label}» не загрузился за 90 секунд; "
                                "основной отчёт продолжен без него."
                            ],
                        )
                        prev = cur.model_copy(update={"period": period.previous})
                    checks[label] = (
                        cur.status.value if prev.status == DataStatus.OK else prev.status.value
                    )
                    limitations.extend(f"{label}, текущий период: {v}" for v in cur.limitations)
                    limitations.extend(f"{label}, предыдущий период: {v}" for v in prev.limitations)
                    if (
                        dim != "device"
                        and mature
                        and health["healthy"]
                        and cur.status == DataStatus.OK
                    ):
                        empty = [
                            r
                            for r in cur.rows
                            if r.totals.conversions == 0
                            and (r.totals.spend or 0) >= threshold
                            and (r.totals.clicks or 0) >= client.targets.minimum_clicks
                        ]
                        if empty:
                            signals.append(
                                Signal(
                                    type=f"{dim}_without_conversions",
                                    level="yellow",
                                    message=f"Разрез «{label}»: есть расход без основных конверсий.",
                                    actual={
                                        "segments": [
                                            {
                                                "name": r.name,
                                                "spend": r.totals.spend,
                                                "conversions": 0,
                                            }
                                            for r in empty[:5]
                                        ],
                                        "count": len(empty),
                                    },
                                    period=period,
                                    evidence="Порог кликов и расхода превышен в указанных сегментах.",
                                    confidence="medium",
                                    sufficient_data=True,
                                    next_check=f"Проверить разрез «{label}» и соответствие трафика основной цели.",
                                )
                            )
                    if dim == "device" and mature and health["healthy"]:
                        for row in drivers(cur, prev):
                            x, y = (
                                Metrics.model_validate(row["current"]),
                                Metrics.model_validate(row["previous"]),
                            )
                            drop = change(x.cr, y.cr)["percent"]
                            if (
                                drop is not None
                                and drop <= -client.targets.cr_drop_percent
                                and min(x.clicks or 0, y.clicks or 0)
                                >= client.targets.minimum_clicks
                                and (y.conversions or 0) >= client.targets.minimum_conversions
                            ):
                                signals.append(
                                    Signal(
                                        type="device_cr_drop",
                                        level="yellow",
                                        message=f"Снизился CR: {row['name']}.",
                                        actual={
                                            "current_cr": x.cr,
                                            "previous_cr": y.cr,
                                            "percent": drop,
                                        },
                                        period=period,
                                        evidence="Достаточный трафик в обоих периодах; CR рассчитан по кликам.",
                                        confidence="medium",
                                        sufficient_data=True,
                                        next_check="Проверить формы и посадочные страницы на этом устройстве.",
                                    )
                                )
            else:
                limitations.append("Краткая сводка: детальные разрезы не проверялись.")
            signals.sort(key=lambda s: 0 if s.level == "red" else 1)
            if len(signals) > 50:
                limitations.append(
                    f"Показаны первые 50 сигналов из {len(signals)}; сузьте период для деталей."
                )
                signals = signals[:50]
            reliable = (
                health["healthy"]
                and previous.direct.status == DataStatus.OK
                and previous.metrica.status == DataStatus.OK
                and (a.clicks or 0) >= client.targets.minimum_clicks
                and current.direct.campaigns_status == DataStatus.OK
                and mature
                and all(v in ("ok", "not_checked") for v in checks.values())
            )
            level = signals[0].level if signals else "green" if reliable else "unknown"
            return ClientReport(
                client_id=client.id,
                client_name=client.name,
                period=period,
                mode=mode,
                status="signals_detected"
                if signals
                else "no_problems_detected"
                if reliable
                else "insufficient",
                level=level,
                current=a,
                previous=b,
                changes=compare(a, b),
                signals=signals,
                drivers=campaign_drivers,
                checks=checks,
                source_status={
                    "Директ": current.direct.status.value,
                    "Метрика": current.metrica.status.value,
                    current.revenue.source: current.revenue.status.value,
                },
                limitations=list(dict.fromkeys(limitations)),
                main_goal_ids=client.metrica.main_goal_ids,
                goal_scope=current.metrica.scope,
                goal_metrics=[
                    {
                        **g,
                        "previous_reaches": next(
                            (
                                p.get("reaches")
                                for p in previous.metrica.goals
                                if (p.get("counter_id"), p["id"]) == (g.get("counter_id"), g["id"])
                            ),
                            None,
                        ),
                    }
                    for g in current.metrica.goals
                    if g.get("reaches") is not None
                ],
                metrica_current={
                    "visits": current.metrica.visits,
                    "users": current.metrica.users,
                    "pageviews": current.metrica.pageviews,
                    "bounce_rate": current.metrica.bounce_rate,
                    "page_depth": current.metrica.page_depth,
                    "avg_visit_duration_seconds": current.metrica.avg_visit_duration_seconds,
                },
                metrica_previous={
                    "visits": previous.metrica.visits,
                    "users": previous.metrica.users,
                    "pageviews": previous.metrica.pageviews,
                    "bounce_rate": previous.metrica.bounce_rate,
                    "page_depth": previous.metrica.page_depth,
                    "avg_visit_duration_seconds": previous.metrica.avg_visit_duration_seconds,
                },
                mock=self.provider.mock,
                generated_at=datetime.now(UTC),
            )

    async def run_check(self, client_ids, period, mode, trigger, *, chat_id, user_id=None):
        period.completed()
        # Fail closed before any integration is called, even in scheduled/internal paths.
        clients = [self.registry.require(chat_id, cid) for cid in dict.fromkeys(client_ids)]
        run_id = await self.repository.begin_run(
            chat_id, [c.id for c in clients], period, mode, trigger, user_id=user_id
        )
        start = monotonic()
        reports, errors = [], []
        try:

            async def analyze_one(client):
                # A batch must not reserve all foreground slots before a manual request arrives.
                if trigger == TriggerSource.SCHEDULE or len(clients) > 1:
                    async with self.schedule_semaphore:
                        return await self.analyze(client, period, mode)
                return await self.analyze(client, period, mode)

            logger.info(
                "Check run started id=%s clients=%s trigger=%s", run_id, len(clients), trigger
            )
            results = await asyncio.gather(
                *(analyze_one(c) for c in clients), return_exceptions=True
            )
            for client, result in zip(clients, results, strict=True):
                if isinstance(result, BaseException):
                    errors.append(
                        f"{client.name}: проверка завершилась ошибкой; остальные клиенты обработаны."
                    )
                else:
                    reports.append(result)
                    if any(v == "unavailable" for v in result.source_status.values()):
                        errors.append(f"{client.name}: один или несколько источников недоступны.")
            text = (
                detailed(reports[0])
                if len(clients) == 1 and reports and mode != CheckMode.SUMMARY
                else compact(reports, period, errors, mode == CheckMode.SUMMARY)
            )
            await self.repository.finish_run(run_id, reports, errors, text, monotonic() - start)
            logger.info(
                "Check run finished id=%s reports=%s errors=%s elapsed=%.1fs",
                run_id,
                len(reports),
                len(errors),
                monotonic() - start,
            )
            return reports, text
        except BaseException:
            await self.repository.finish_run(
                run_id, reports, ["Проверка прервана."], "", monotonic() - start, status="failed"
            )
            raise
