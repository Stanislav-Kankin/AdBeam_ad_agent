import asyncio
import hashlib
import json
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
from app.reporting.formatter import brief, compact
from app.storage.repository import safe_json

logger = logging.getLogger(__name__)
METRICA_REPORT_CACHE_VERSION = 2
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


def report_level(client, signals, current, previous, reliable):
    if any(signal.level == "red" for signal in signals):
        return "red"
    cpa_delta = change(current.cpa, previous.cpa)["percent"]
    stable_cpa = bool(
        client.targets.target_cpa
        and current.cpa is not None
        and previous.cpa is not None
        and cpa_delta is not None
        and abs(Decimal(cpa_delta)) <= Decimal(str(client.targets.kpi_change_tolerance_percent))
        and current.cpa
        <= client.targets.target_cpa * (1 + Decimal(str(client.targets.cpa_excess_percent)) / 100)
    )
    contextual = {"campaign_states"}
    if stable_cpa:
        contextual.update(("spend_change", "cpc_change"))
    if any(signal.type not in contextual for signal in signals):
        return "yellow"
    return "green" if reliable else "unknown"


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
                candidates = []
                for client_index, client in enumerate(clients):
                    for dimension_index, dimension in enumerate(WAREHOUSE_DIMENSIONS):
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
                        candidates.append((page, client_index, dimension_index, client, dimension))
                if not candidates:
                    continue
                page, _, _, client, dimension = min(candidates, key=lambda value: value[:3])
                logger.info(
                    "Dimension warehouse warm client=%s day=%s dimension=%s page=%s",
                    client.id,
                    day,
                    dimension,
                    page + 1,
                )
                rows, complete = await self.provider.breakdown_page(client, period, dimension, page)
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

    async def dynamics(self, client, period):
        async def load(date_range):
            chunks = []
            start = date_range.start
            while start <= date_range.end:
                end = min(start + timedelta(days=29), date_range.end)
                chunks.append(DateRange(start=start, end=end))
                start = end + timedelta(days=1)
            results = []
            for chunk in chunks:
                results.append(await self.breakdown(client, chunk, "date"))
            rows = [row for result in results for row in result.rows]
            statuses = {result.status for result in results}
            if DataStatus.UNAVAILABLE in statuses:
                status = DataStatus.UNAVAILABLE
            elif DataStatus.INSUFFICIENT in statuses:
                status = DataStatus.INSUFFICIENT
            elif rows:
                status = DataStatus.OK
            else:
                status = DataStatus.EMPTY
            return DirectData(
                status=status,
                period=date_range,
                rows=rows,
                totals=aggregate([row.totals for row in rows]),
                limitations=list(
                    dict.fromkeys(item for result in results for item in result.limitations)
                ),
            )

        async with self.semaphore:
            return await asyncio.gather(
                load(period.current),
                load(period.previous),
            )

    async def audience(self, client, period):
        cached = await self.repository.cached_analysis(client.id, period.current, "audience")
        if cached is not None:
            logger.info("Audience cache hit client=%s", client.id)
            return cached
        age, gender, income, interests = await asyncio.gather(
            self.breakdown(client, period.current, "age"),
            self.breakdown(client, period.current, "gender"),
            self.breakdown(client, period.current, "income"),
            self.provider.audience_interests(client, period.current),
        )
        result = {
            "client_id": client.id,
            "period": period.current.model_dump(mode="json"),
            "direct": {
                name: value.model_dump(mode="json")
                for name, value in (("age", age), ("gender", gender), ("income", income))
            },
            "interests": interests,
        }
        await self.repository.save_analysis(client.id, period.current, "audience", result)
        return result

    async def metrica_report(
        self, client, period, report_type, *, goal_ids=None, campaign_ids=None, top_n=20
    ):
        options = {
            "cache_version": METRICA_REPORT_CACHE_VERSION,
            "report": report_type,
            "goals": sorted(goal_ids or client.metrica.main_goal_ids),
            "campaigns": sorted(campaign_ids or []),
            "top_n": top_n,
        }

        async def load(date_range):
            digest = hashlib.sha256(
                json.dumps(options, sort_keys=True).encode("utf-8")
            ).hexdigest()[:8]
            kind = "mr" + digest
            cached = await self.repository.cached_analysis(client.id, date_range, kind)
            if cached is not None:
                logger.info(
                    "Metrica report cache hit client=%s report=%s period=%s..%s",
                    client.id,
                    report_type,
                    date_range.start,
                    date_range.end,
                )
                return cached
            result = await self.provider.metrica_direct_report(
                client,
                date_range,
                report_type,
                goal_ids=goal_ids,
                campaign_ids=campaign_ids,
                limit=top_n,
            )
            result = safe_json(result)
            if result.get("status") != "unavailable":
                await self.repository.save_analysis(
                    client.id, date_range, kind, result, ttl_hours=6
                )
            return result

        current, previous = await asyncio.gather(load(period.current), load(period.previous))
        now = {row["key"]: row for row in current.get("rows", [])}
        before = {row["key"]: row for row in previous.get("rows", [])}
        rows = []
        for key in now.keys() | before.keys():
            a, b = now.get(key), before.get(key)
            current_metrics = (a or {}).get("metrics", {})
            previous_metrics = (b or {}).get("metrics", {})
            names = current_metrics.keys() | previous_metrics.keys()
            rows.append(
                {
                    "key": key,
                    "dimensions": (a or b)["dimensions"],
                    "current": current_metrics,
                    "previous": previous_metrics,
                    "changes": {
                        name: change(current_metrics.get(name), previous_metrics.get(name))
                        for name in names
                    },
                }
            )
        if report_type == "campaign":
            direct_current, direct_previous = await self.snapshots(client, period)
            direct_now = {row.id: row for row in direct_current.direct.rows}
            direct_before = {row.id: row for row in direct_previous.direct.rows}
            if campaign_ids:
                selected = set(campaign_ids)
                direct_now = {key: value for key, value in direct_now.items() if key in selected}
                direct_before = {
                    key: value for key, value in direct_before.items() if key in selected
                }
            metrica_ids = {row["dimensions"][0]["id"] for row in rows}
            for campaign_id in (direct_now.keys() | direct_before.keys()) - metrica_ids:
                source = direct_now.get(campaign_id) or direct_before[campaign_id]
                rows.append(
                    {
                        "key": campaign_id,
                        "dimensions": [{"id": campaign_id, "name": source.name}],
                        "current": {},
                        "previous": {},
                        "changes": {},
                    }
                )
            for row in rows:
                campaign_id = row["dimensions"][0]["id"]
                a, b = direct_now.get(campaign_id), direct_before.get(campaign_id)
                current_metrics = calculate(a.totals).model_dump(mode="json") if a else {}
                previous_metrics = calculate(b.totals).model_dump(mode="json") if b else {}
                row["direct"] = {
                    "current": current_metrics,
                    "previous": previous_metrics,
                    "changes": {
                        name: change(current_metrics.get(name), previous_metrics.get(name))
                        for name in current_metrics.keys() | previous_metrics.keys()
                    },
                }
            rows.sort(
                key=lambda row: (
                    row["direct"]["current"].get("spend") or row["current"].get("visits") or 0
                ),
                reverse=True,
            )
        else:
            rows.sort(key=lambda row: row["current"].get("visits") or 0, reverse=True)
        return {
            "status": current.get("status", "unavailable"),
            "report": report_type,
            "period": period.model_dump(mode="json"),
            "counter_id": current.get("counter_id"),
            "attribution": current.get("attribution"),
            "goals": current.get("goals", []),
            "rows": rows[:top_n],
            "total_rows": max(current.get("total_rows", len(now)), len(rows)),
            "truncated": current.get("truncated", False) or len(rows) > top_n,
            "sampled": current.get("sampled", False) or previous.get("sampled", False),
            "limitations": list(
                dict.fromkeys(
                    [
                        *current.get("limitations", []),
                        *previous.get("limitations", []),
                    ]
                )
            ),
        }

    async def resolve_campaign_references(self, client, period, references):
        """Resolve campaign names without relying on prior model tool context."""
        if not references:
            return {"ids": [], "unresolved": [], "ambiguous": {}}
        current, previous = await asyncio.gather(
            self.breakdown(client, period.current, "campaign"),
            self.breakdown(client, period.previous, "campaign"),
        )
        campaigns = {}
        for row in [*current.rows, *previous.rows]:
            campaigns.setdefault(str(row.id), row.name)
        resolved, unresolved, ambiguous = [], [], {}
        for reference in references:
            value = str(reference).strip()
            if value.isdigit():
                resolved.append(value)
                continue
            needle = value.casefold()
            exact = [
                campaign_id for campaign_id, name in campaigns.items() if name.casefold() == needle
            ]
            matches = exact or [
                campaign_id for campaign_id, name in campaigns.items() if needle in name.casefold()
            ]
            matches = list(dict.fromkeys(matches))
            if len(matches) == 1:
                resolved.append(matches[0])
            elif not matches:
                unresolved.append(value)
            else:
                ambiguous[value] = [
                    {"id": campaign_id, "name": campaigns[campaign_id]}
                    for campaign_id in matches[:5]
                ]
        return {
            "ids": list(dict.fromkeys(resolved)),
            "unresolved": unresolved,
            "ambiguous": ambiguous,
        }

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
                        for key in ("current", "previous"):
                            row[key]["conversions"] = None
                            row[key]["cr"] = None
                            row[key]["cpa"] = None
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
            # When CPA is the configured KPI and remains stable, traffic-volume changes and
            # currently stopped legacy campaigns stay useful context but do not color the account.
            level = report_level(client, signals, a, b, reliable)
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
                targets={
                    "target_cpa": client.targets.target_cpa,
                    "target_drr": client.targets.target_drr,
                    "kpi_change_tolerance_percent": client.targets.kpi_change_tolerance_percent,
                },
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
                brief(reports[0])
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
