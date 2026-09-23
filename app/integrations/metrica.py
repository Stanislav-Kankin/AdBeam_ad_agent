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
DIRECT_REPORT_DIMENSIONS = {
    "campaign": ("DirectClickOrder",),
    "ad": ("DirectClickOrder", "DirectClickBanner"),
    "condition": ("DirectClickOrder", "DirectPhraseOrCond"),
    "search_phrase": ("DirectClickOrder", "DirectSearchPhrase"),
    "platform": ("DirectClickOrder", "DirectPlatformType", "DirectPlatform"),
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

    async def audience_interests(self, client, period):
        counters = client.metrica.selected_counter_ids()
        if not counters:
            counters = await campaign_counters(self.transport, client)
        rows, limitations = [], []
        for counter_id in counters[:3]:
            data = await self.transport.json(
                "metrica",
                "GET",
                BASE_URL + "/stat/v1/data",
                headers=self.headers(client),
                params={
                    "ids": counter_id,
                    "date1": str(period.start),
                    "date2": str(period.end),
                    "preset": "interests2",
                    "dimensions": "ym:s:interest2d1,ym:s:interest2d2,ym:s:interest2d3",
                    "metrics": "ym:s:visits,ym:s:users,ym:s:affinityIndexInterests2",
                    "sort": "-ym:s:affinityIndexInterests2",
                    "accuracy": "full",
                    "lang": "ru",
                    "limit": 20,
                },
            )
            if data.get("contains_sensitive_data"):
                limitations.append(
                    f"Счётчик {counter_id}: часть аудиторных данных скрыта правилами обезличивания."
                )
            for item in data.get("data", []):
                dimensions = [
                    str(value.get("name") or value.get("id") or "").strip()
                    for value in item.get("dimensions", [])
                    if isinstance(value, dict)
                ]
                metrics = item.get("metrics", [])
                if not any(dimensions) or len(metrics) < 3:
                    continue
                rows.append(
                    {
                        "counter_id": counter_id,
                        "name": " → ".join(value for value in dimensions if value),
                        "visits": number(metrics[0]),
                        "users": number(metrics[1]),
                        "affinity": number(metrics[2]),
                    }
                )
        if len(counters) > 3:
            limitations.append(
                f"Показаны интересы первых трёх из {len(counters)} связанных счётчиков."
            )
        rows.sort(key=lambda item: item.get("affinity") or 0, reverse=True)
        return {
            "status": "ok" if rows else "no_data",
            "scope": "site_counter",
            "rows": rows[:20],
            "limitations": limitations,
        }

    async def direct_report(
        self, client, period, report_type, *, goal_ids=None, campaign_ids=None, limit=20
    ):
        """Build a validated campaign drill-down from Metrica's Reports API."""
        if report_type not in DIRECT_REPORT_DIMENSIONS:
            raise ValueError("unsupported_metrica_report")
        campaign_ids = list(dict.fromkeys(str(value) for value in (campaign_ids or [])))
        if len(campaign_ids) > 100 or any(not value.isdigit() for value in campaign_ids):
            raise ValueError("invalid_campaign_ids")
        configured = client.metrica.selected_counter_ids()
        counters = configured or await campaign_counters(self.transport, client)
        if not counters:
            return {
                "status": "not_checked",
                "rows": [],
                "limitations": ["В кампаниях не найден счётчик Метрики."],
            }
        counter_id = counters[0]
        limitations = []
        if len(counters) > 1:
            limitations.append(
                f"Для отчёта использован основной счётчик {counter_id}; "
                f"ещё {len(counters) - 1} счётчиков требуют отдельного отчёта."
            )
        goals_data = await self.transport.json(
            "metrica",
            "GET",
            f"{BASE_URL}/management/v1/counter/{counter_id}/goals",
            headers=self.headers(client),
        )
        raw_goals = goals_data.get("goals")
        if not isinstance(raw_goals, list):
            raise IntegrationError("metrica", "invalid_goals_response")
        available = {
            str(value["id"]): redact(str(value.get("name") or value["id"]))[:150]
            for value in raw_goals
            if isinstance(value, dict) and "id" in value
        }
        requested = list(
            dict.fromkeys(str(value) for value in (goal_ids or client.metrica.main_goal_ids))
        )[:10]
        missing = [value for value in requested if value not in available]
        goals = [value for value in requested if value in available]
        if missing:
            limitations.append("Цели недоступны на выбранном счётчике: " + ", ".join(missing))
        if not goals:
            limitations.append(
                "Основные цели не выбраны; отчёт содержит трафик и поведение без конверсий."
            )

        attribution = ATTRIBUTIONS[client.direct.attribution_model]
        dimensions = [f"ym:s:{attribution}{name}" for name in DIRECT_REPORT_DIMENSIONS[report_type]]
        base_metrics = [
            "ym:s:visits",
            "ym:s:users",
            "ym:s:bounceRate",
            "ym:s:pageDepth",
            "ym:s:avgVisitDurationSeconds",
        ]
        batches = [base_metrics]
        for start in range(0, len(goals), 9):
            batch = ["ym:s:visits"]
            for goal_id in goals[start : start + 9]:
                batch.extend((f"ym:s:goal{goal_id}visits", f"ym:s:goal{goal_id}conversionRate"))
            batches.append(batch)

        metric_names = {
            "ym:s:visits": "visits",
            "ym:s:users": "users",
            "ym:s:bounceRate": "bounce_rate",
            "ym:s:pageDepth": "page_depth",
            "ym:s:avgVisitDurationSeconds": "avg_visit_duration_seconds",
        }
        for goal_id in goals:
            metric_names[f"ym:s:goal{goal_id}visits"] = f"goal_{goal_id}_visits"
            metric_names[f"ym:s:goal{goal_id}conversionRate"] = f"goal_{goal_id}_conversion_rate"

        merged, sampled, sample_share, total_rows = {}, False, number(1), 0
        for metrics in batches:
            params = {
                "ids": counter_id,
                "date1": str(period.start),
                "date2": str(period.end),
                "dimensions": ",".join(dimensions),
                "metrics": ",".join(metrics),
                "sort": "-ym:s:visits",
                "accuracy": "full",
                "include_undefined": "true",
                "lang": "ru",
                "limit": 1000
                if report_type == "campaign" and not campaign_ids
                else min(max(int(limit), 1), 50),
            }
            if campaign_ids:
                params["filters"] = (
                    f"ym:s:{attribution}DirectClickOrder=.(" + ",".join(campaign_ids) + ")"
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
            sampled |= bool(data.get("sampled"))
            if data.get("sample_share") is not None:
                sample_share = min(sample_share, number(data["sample_share"]))
            total_rows = max(total_rows, int(data.get("total_rows") or 0))
            for item in data.get("data", []):
                raw_dimensions = item.get("dimensions", [])
                values = item.get("metrics", [])
                if len(raw_dimensions) != len(dimensions) or len(values) != len(metrics):
                    continue
                cleaned = [
                    {
                        "id": str(value.get("id") or value.get("name") or "undefined")[:200],
                        "name": redact(
                            str(value.get("name") or value.get("id") or "Не определено")
                        )[:200],
                    }
                    for value in raw_dimensions
                ]
                key = "|".join(value["id"] for value in cleaned)
                row = merged.setdefault(key, {"key": key, "dimensions": cleaned, "metrics": {}})
                for name, value in zip(metrics, values, strict=True):
                    row["metrics"][metric_names[name]] = number(value)

        if sampled:
            limitations.append(f"Метрика применила семплирование: доля {sample_share}.")
        rows = sorted(
            merged.values(), key=lambda value: value["metrics"].get("visits") or 0, reverse=True
        )
        return {
            "status": "insufficient" if limitations else "ok" if rows else "no_data",
            "counter_id": counter_id,
            "report": report_type,
            "attribution": attribution,
            "goals": [{"id": value, "name": available[value]} for value in goals],
            "rows": rows,
            "total_rows": total_rows,
            "truncated": total_rows > len(rows),
            "sampled": sampled,
            "sample_share": sample_share,
            "limitations": limitations,
        }

    async def overview(self, client, period, campaign_ids, *, budget=165):
        deadline = monotonic() + budget
        configured = client.metrica.selected_counter_ids()
        if len(configured) == 1:
            scoped = client.model_copy(
                update={"metrica": client.metrica.model_copy(update={"counter_id": configured[0]})}
            )
            return await self._overview(
                scoped,
                period,
                campaign_ids,
                all_goals=not client.metrica.main_goal_ids,
                deadline=deadline,
            )
        counters = configured or await campaign_counters(self.transport, client)
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
                    scoped,
                    period,
                    campaign_ids,
                    all_goals=not configured or not client.metrica.main_goal_ids,
                    deadline=deadline,
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
            users=reports[0].users if len(reports) == 1 else None,
            pageviews=reports[0].pageviews if len(reports) == 1 else None,
            bounce_rate=reports[0].bounce_rate if len(reports) == 1 else None,
            page_depth=reports[0].page_depth if len(reports) == 1 else None,
            avg_visit_duration_seconds=reports[0].avg_visit_duration_seconds
            if len(reports) == 1
            else None,
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
        base_metrics = [
            "ym:s:visits",
            "ym:s:users",
            "ym:s:pageviews",
            "ym:s:bounceRate",
            "ym:s:pageDepth",
            "ym:s:avgVisitDurationSeconds",
        ]
        metrics = [*base_metrics, *[f"ym:s:goal{g}reaches" for g in queried]]
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
                "reaches": str(values[queried.index(gid) + len(base_metrics)])
                if gid in queried and values[queried.index(gid) + len(base_metrics)] is not None
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
            users=int(values[1]) if values[1] is not None else None,
            pageviews=int(values[2]) if values[2] is not None else None,
            bounce_rate=values[3],
            page_depth=values[4],
            avg_visit_duration_seconds=values[5],
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
