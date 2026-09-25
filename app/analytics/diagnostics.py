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
from app.analytics.rules import (
    CONTEXT_SIGNALS,
    VOLUME_SIGNALS,
    conversion_maturity,
    evaluate,
    kpi_stable,
    main_kpi,
    tracking_health,
)
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
from app.reporting.formatter import card, compact
from app.storage.repository import safe_json

logger = logging.getLogger(__name__)
METRICA_REPORT_CACHE_VERSION = 2
MIN_BEST_CONVERSIONS = 5
EXTRA_GOALS_LIMIT = 20  # two more Direct report batches at most
MAX_GOALS_SHOWN = 8
GOALS_REFRESH_SECONDS = 12 * 3600
WAREHOUSE_DIMENSIONS = ("device", "geo", "search", "placement")


def snapshot_metrics(snapshot, *, healthy=True):
    totals = snapshot.direct.totals.model_copy(deep=True)
    if snapshot.direct.status != DataStatus.OK:
        totals = Totals()
    if not healthy:
        totals.conversions = None
    revenue = snapshot.revenue
    totals.revenue = revenue.amount if revenue.status == DataStatus.OK else None
    metrics = calculate(totals)
    if not revenue.comparable or revenue.period != snapshot.direct.period:
        metrics.drr = None
    return metrics


def amount(value) -> Decimal:
    """Numeric sort key. JSON payloads carry Decimals as strings, so comparing them
    directly sorted "9000" above "10000" and failed with TypeError against a 0."""
    return Decimal(str(value)) if value not in (None, "") else Decimal(0)


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
    # The account status follows the project KPI. When it is stable or improving,
    # traffic volume and funnel shifts stay in the report as context, not as alerts.
    contextual = CONTEXT_SIGNALS | (
        VOLUME_SIGNALS if kpi_stable(client, current, previous) else frozenset()
    )
    if any(signal.type not in contextual for signal in signals):
        return "yellow"
    return "green" if reliable else "unknown"


class CheckService:
    def __init__(self, registry, provider, repository):
        self.registry, self.provider, self.repository = registry, provider, repository
        self.semaphore = asyncio.Semaphore(3)
        self.schedule_semaphore = asyncio.Semaphore(1)
        self.goals_checked = {}

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
        clients = [await self.ensure_goals(client) for client in clients]
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
        clients = [await self.ensure_goals(client) for client in clients]
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
        def failed(payload):
            return (
                any(
                    value.get("status") == DataStatus.UNAVAILABLE.value
                    for value in payload.get("direct", {}).values()
                )
                or payload.get("interests", {}).get("status") == "unavailable"
            )

        cached = await self.repository.cached_analysis(client.id, period.current, "audience")
        # A cached failure would hide the audience for hours; retry it instead.
        if cached is not None and not failed(cached):
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
        if failed(result):
            logger.warning(
                "Audience partly unavailable client=%s limitations=%s",
                client.id,
                [v.limitations for v in (age, gender, income)],
            )
        else:
            await self.repository.save_analysis(client.id, period.current, "audience", result)
        return result

    async def ensure_goals(self, client):
        """Nobody chose main goals: take each campaign's primary goal from its settings.
        A person's choice always wins; automatic goals are re-read every 12 hours so
        the bot follows goal changes made in the ad account."""
        if client.direct.main_goal_ids and client.direct.goals_source == "manual":
            return client
        checked = self.goals_checked.get(client.id)
        if checked is not None and monotonic() - checked < GOALS_REFRESH_SECONDS:
            return client
        self.goals_checked[client.id] = monotonic()
        try:
            settings = await self.provider.campaign_goals(client)
        except Exception as exc:
            logger.warning("Automatic goals failed client=%s error=%s", client.id, exc)
            # Retry in ten minutes rather than waiting for the next 12-hour refresh.
            self.goals_checked[client.id] = monotonic() - GOALS_REFRESH_SECONDS + 600
            return client
        # Goals that drive more campaigns first; the Reports API takes at most ten.
        counts = {}
        for row in settings.values():
            if row.get("primary_goal_id"):
                counts[row["primary_goal_id"]] = counts.get(row["primary_goal_id"], 0) + 1
        goals = sorted(counts, key=lambda goal: (-counts[goal], goal))[:10]
        if not goals or (goals == client.direct.main_goal_ids):
            return client
        logger.info("Automatic goals client=%s goals=%s", client.id, goals)
        updated = await self.repository.save_client_preferences(
            client, goal_ids=goals, goals_source="campaigns"
        )
        self.registry.clients[client.id] = updated
        return updated

    async def campaign_goal_performance(
        self, client, start, end, *, top_n=20, segment=None, campaign_ids=None
    ):
        """Campaigns judged by the goals configured inside them (key goals and the
        strategy goal). Works without selected main goals and without Metrica, and
        covers up to a year by summing additive Direct totals over <=90-day chunks.
        segment (gender/age/income) adds who converted on those goals, next to the
        demographic bid adjustments the campaigns were set up with."""
        base = {"period": {"start": str(start), "end": str(end)}, "source": "Direct Reports"}
        try:
            settings = await self.provider.campaign_goals(client)
        except Exception as exc:
            logger.warning("Campaign goals failed client=%s error=%s", client.id, exc)
            return {**base, "status": "unavailable", "limitations": [f"direct: {exc}"]}
        goal_ids = sorted({goal for row in settings.values() for goal in row["goal_ids"]})
        names = {
            str(goal["id"]): goal["name"]
            for counter in await self.repository.client_counters(client.id)
            for goal in counter.get("goals") or []
        }
        blind = [row for row in settings.values() if not row.get("primary_goal_id")]
        if blind or set(goal_ids) - names.keys():
            counters = sorted({c for row in settings.values() for c in row.get("counter_ids", [])})
            names.update(await self.provider.goal_names(client, counters))
        if blind:
            # Some campaigns (often Master campaigns) expose no goal through the API.
            # Their goal may be one no other campaign uses, so request the counters'
            # goals too; otherwise such a campaign could never win.
            goal_ids = sorted({*goal_ids, *list(names)[:EXTRA_GOALS_LIMIT]})
        if not goal_ids:
            return {
                **base,
                "status": "no_campaign_goals",
                "limitations": [
                    "Ни в одной кампании не заданы ключевые цели или цель стратегии; "
                    "сравнивать по целям кампаний нечем."
                ],
            }
        chunks, cursor = [], start
        while cursor <= end:
            chunk_end = min(end, cursor + timedelta(days=89))
            chunks.append(DateRange(start=cursor, end=chunk_end))
            cursor = chunk_end + timedelta(days=1)

        async def load(split):
            """Rows summed over all chunks, keyed like goal_report; None on failure."""
            kind = (
                "cg" + hashlib.sha256(json.dumps([goal_ids, split]).encode("utf-8")).hexdigest()[:8]
            )
            summed = {}
            for chunk in chunks:
                rows = await self.repository.cached_analysis(client.id, chunk, kind)
                if rows is None:
                    try:
                        rows = safe_json(
                            await self.provider.goal_report(client, chunk, goal_ids, split)
                        )
                    except Exception as exc:
                        logger.warning(
                            "Goal report failed client=%s segment=%s period=%s..%s error=%s",
                            client.id,
                            split,
                            chunk.start,
                            chunk.end,
                            exc,
                        )
                        limitations.append(f"direct: {exc} ({chunk.label()})")
                        return None
                    await self.repository.save_analysis(client.id, chunk, kind, rows, ttl_hours=6)
                for key, row in rows.items():
                    target = summed.setdefault(
                        key,
                        {
                            "campaign_id": row.get("campaign_id", key),
                            "segment": row.get("segment"),
                            "name": row["name"],
                            "spend": Decimal(0),
                            "clicks": 0,
                            "impressions": 0,
                            "goals": {},
                        },
                    )
                    target["spend"] += amount(row["spend"])
                    target["clicks"] += int(row["clicks"] or 0)
                    target["impressions"] += int(row["impressions"] or 0)
                    for goal, value in row["goals"].items():
                        target["goals"][goal] = target["goals"].get(goal, Decimal(0)) + amount(
                            value
                        )
            return summed

        limitations = []
        totals = await load(None)
        if totals is None:
            return {**base, "status": "unavailable", "limitations": limitations}
        if campaign_ids:
            wanted = set(campaign_ids)
            totals = {
                key: row for key, row in totals.items() if key in wanted or row["name"] in wanted
            }

        def goal_name(goal):
            return names.get(goal) or f"цель {goal} (название недоступно)"

        def ratio(numerator, denominator, scale=1):
            return (
                (Decimal(numerator) * scale / Decimal(denominator)).quantize(Decimal("0.01"))
                if denominator
                else None
            )

        campaigns = []
        for campaign_id, row in totals.items():
            if not row["spend"] and not row["clicks"]:
                continue
            meta = settings.get(campaign_id, {})
            primary = meta.get("primary_goal_id")
            conversions = row["goals"].get(primary, Decimal(0)) if primary else None
            # Goals the campaign converts on although they are not in its settings,
            # e.g. a Master campaign whose goal the API did not expose.
            observed = sorted(
                (
                    (goal, value)
                    for goal, value in row["goals"].items()
                    if value and goal != primary
                ),
                key=lambda item: -item[1],
            )[:3]
            campaigns.append(
                {
                    "id": campaign_id,
                    "name": meta.get("name") or row["name"],
                    "state": meta.get("state", ""),
                    "type": meta.get("type", ""),
                    "spend": row["spend"],
                    "clicks": row["clicks"],
                    "primary_goal": {"id": primary, "name": goal_name(primary)}
                    if primary
                    else None,
                    "conversions": conversions,
                    "cpa": ratio(row["spend"], conversions) if primary else None,
                    "cr": ratio(conversions, row["clicks"], 100) if primary else None,
                    "other_goals": [
                        {
                            "id": goal,
                            "name": goal_name(goal),
                            "conversions": value,
                            "in_settings": goal in meta.get("goal_ids", []),
                        }
                        for goal, value in observed
                    ],
                }
            )
        campaigns.sort(key=lambda row: row["spend"], reverse=True)

        # Direct attributes a goal's conversions to every campaign that brought them,
        # whatever the campaign settings, so campaigns are compared goal by goal. Every
        # campaign with conversions takes part, configured or not, so a campaign whose
        # settings the API did not expose is not silently excluded. Goals that are some
        # campaign's primary goal come first; micro goals follow and are capped.
        primaries = {}
        for row in campaigns:
            if row["primary_goal"]:
                primaries.setdefault(row["primary_goal"]["id"], []).append(row["id"])
        volume = {
            goal: sum((row["goals"].get(goal) or Decimal(0) for row in totals.values()), Decimal(0))
            for goal in goal_ids
        }
        order = sorted(
            (goal for goal in goal_ids if goal in primaries or volume[goal]),
            key=lambda goal: (-len(primaries.get(goal, [])), -volume[goal]),
        )[:MAX_GOALS_SHOWN]
        by_goal = []
        for goal in order:
            owners = primaries.get(goal, [])
            ranked = []
            for campaign_id, row in totals.items():
                value = row["goals"].get(goal) or Decimal(0)
                if not value and campaign_id not in owners:
                    continue
                ranked.append(
                    {
                        "id": campaign_id,
                        "name": settings.get(campaign_id, {}).get("name") or row["name"],
                        "conversions": value,
                        "cpa": ratio(row["spend"], value),
                        "spend": row["spend"],
                        "goal_is_primary": campaign_id in owners,
                        "goal_in_settings": goal
                        in settings.get(campaign_id, {}).get("goal_ids", []),
                    }
                )
            total = sum((r["conversions"] for r in ranked), Decimal(0))
            # A campaign with a couple of conversions must not win on a lucky CPA.
            floor = max(Decimal(MIN_BEST_CONVERSIONS), total * Decimal("0.05"))
            eligible = [r for r in ranked if r["conversions"] >= floor and r["cpa"] is not None]
            ranked.sort(key=lambda r: -r["conversions"])
            by_goal.append(
                {
                    "goal_id": goal,
                    "name": goal_name(goal),
                    "primary_for_campaigns": len(owners),
                    "conversions": total,
                    "best_by_cpa": min(eligible, key=lambda r: r["cpa"]) if eligible else None,
                    "most_conversions": ranked[0] if ranked and ranked[0]["conversions"] else None,
                    "campaigns": ranked[:top_n],
                }
            )
        without = [row for row in campaigns if row["primary_goal"] is None]
        unlisted = [row["id"] for row in without if row["id"] not in settings]
        if unlisted:
            # Spend in Reports but absent from Campaigns.get: their settings (and goals)
            # cannot be read at all, which is what Master campaigns seem to do.
            logger.info(
                "Campaigns missing from Campaigns.get client=%s ids=%s", client.id, unlisted[:20]
            )
        if without:
            limitations.append(
                f"У {len(without)} кампаний Директ не отдал цель в настройках; они всё равно "
                "участвуют в сравнении по целям, на которые у них есть конверсии."
            )
        if len(chunks) > 1:
            limitations.append(
                f"Период {(end - start).days + 1} дн. собран из {len(chunks)} отчётов Директа "
                "по 90 дней и суммирован."
            )
        audience = None
        if segment:
            audience = await self.goal_segments(
                client, load, segment, campaigns[:top_n], settings, limitations, ratio
            )
        return safe_json(
            {
                **base,
                "status": "ok",
                "audience": audience,
                "attribution": client.direct.attribution_model,
                "by_goal": by_goal,
                "campaigns": campaigns[:top_n],
                "total_campaigns": len(campaigns),
                "limitations": limitations,
            }
        )

    async def goal_segments(self, client, load, segment, campaigns, settings, limitations, ratio):
        """Who converted on each campaign's own goals, split by gender/age/income,
        plus the demographic bid adjustments set in those campaigns."""
        rows = await load(segment)
        if rows is None:
            return {"dimension": segment, "status": "unavailable"}
        selected = {row["id"] for row in campaigns if row["primary_goal"]}

        def summary(items):
            clicks = sum(item["clicks"] for item in items) or 0
            conversions = sum((item["conversions"] for item in items), Decimal(0))
            grouped = {}
            for item in items:
                target = grouped.setdefault(
                    item["segment"],
                    {
                        "segment": item["segment"],
                        "clicks": 0,
                        "spend": Decimal(0),
                        "conversions": Decimal(0),
                    },
                )
                target["clicks"] += item["clicks"]
                target["spend"] += item["spend"]
                target["conversions"] += item["conversions"]
            result = []
            for value in grouped.values():
                value["clicks_share_percent"] = ratio(value["clicks"], clicks, 100)
                value["conversions_share_percent"] = ratio(value["conversions"], conversions, 100)
                value["cpa"] = ratio(value["spend"], value["conversions"])
                result.append(value)
            return sorted(result, key=lambda v: v["conversions"], reverse=True)

        items = []
        for row in rows.values():
            if row["campaign_id"] not in selected:
                continue
            primary = settings.get(row["campaign_id"], {}).get("primary_goal_id")
            items.append(
                {
                    "campaign_id": row["campaign_id"],
                    "segment": row["segment"],
                    "clicks": row["clicks"],
                    "spend": row["spend"],
                    "conversions": row["goals"].get(primary, Decimal(0)),
                }
            )
        by_campaign = [
            {
                "id": campaign["id"],
                "name": campaign["name"],
                "rows": summary([i for i in items if i["campaign_id"] == campaign["id"]]),
            }
            for campaign in campaigns
            if campaign["id"] in selected
        ][:5]
        adjustments = None
        if segment in ("gender", "age") and selected:
            try:
                adjustments = await self.provider.demographic_adjustments(client, sorted(selected))
            except Exception as exc:
                logger.warning("Bid adjustments failed client=%s error=%s", client.id, exc)
                limitations.append("Корректировки ставок по полу и возрасту не загрузились.")
        limitations.append(
            "Пол и возраст в Директе — оценка Яндекса по профилю пользователя, а не анкета; "
            "UNKNOWN — пользователи, для которых оценки нет."
        )
        return {
            "dimension": segment,
            "status": "ok",
            "total": summary(items),
            "by_campaign": by_campaign,
            # bid_percent: 100 = no change, 0 = segment excluded (-100%).
            "targeting_adjustments": adjustments,
        }

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
                key=lambda row: amount(
                    row["direct"]["current"].get("spend") or row["current"].get("visits")
                ),
                reverse=True,
            )
        else:
            rows.sort(key=lambda row: amount(row["current"].get("visits")), reverse=True)
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
            mature, fresh_days = conversion_maturity(period.current, client.targets)
            if not mature:
                limitations.append(
                    f"Конверсии могут дополняться {client.targets.conversion_delay_days} дн.; сигналы CPA/CR и расхода без конверсий подавлены."
                )
            elif fresh_days:
                limitations.append(
                    f"Конверсии могут дополняться за последние {fresh_days} дн. периода; "
                    "CPA и CR предварительные и могут немного улучшиться."
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
            if previous.direct.status != DataStatus.OK:
                limitations.append(
                    "Предыдущий период Директа неполный или недоступен; сравнение ограничено."
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
                # A segment only counts as spending "without conversions" once it spent the
                # price of several average conversions of this account: 3 000 ₽ is noise
                # for an account with a 2 000 ₽ CPA and millions in spend.
                if a.cpa is not None:
                    threshold = max(
                        threshold, a.cpa * Decimal(str(client.targets.no_conversion_cpa_multiple))
                    )
                if mature and health["healthy"]:
                    for row in current.direct.rows:
                        if (
                            row.totals.conversions == 0
                            and (row.totals.spend or 0) >= threshold
                            and (row.totals.clicks or 0) >= client.targets.minimum_clicks
                        ):
                            share = row.totals.spend / a.spend * 100 if a.spend else Decimal(100)
                            signals.append(
                                Signal(
                                    type="campaign_without_conversions",
                                    # Critical only when it burns a real part of the budget.
                                    level="red" if share >= 10 else "yellow",
                                    message=f"Кампания «{row.name}» расходует без основных конверсий.",
                                    actual={
                                        "campaign_id": row.id,
                                        "name": row.name,
                                        "spend": row.totals.spend,
                                        "share_percent": share,
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
            # Site-level Metrica data is context; it does not decide Direct KPI reliability.
            reliable = (
                health["healthy"]
                and previous.direct.status == DataStatus.OK
                and (a.clicks or 0) >= client.targets.minimum_clicks
                and current.direct.campaigns_status == DataStatus.OK
                and mature
                and all(
                    v in ("ok", "not_checked")
                    for k, v in checks.items()
                    if k not in ("основные цели", "доступность")
                )
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
                goals_source=client.direct.goals_source,
                targets={
                    "kpi": main_kpi(client),
                    "kpi_stable": kpi_stable(client, a, b),
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
        clients = [await self.ensure_goals(client) for client in clients]
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
                card(reports[0])
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
