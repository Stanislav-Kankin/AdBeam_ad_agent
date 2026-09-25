"""Universal Metrica and Direct reports composed by the agent.

The model chooses metrics, dimensions, fields and filters; this layer enforces what it
may not choose: the client's own counters only, bounded size, a completed period, and
it computes period-over-period changes itself so the model never does arithmetic.
"""

import hashlib
import json
import logging
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace

from app.analytics.metrics import change
from app.integrations.http import IntegrationError
from app.storage.repository import safe_json

logger = logging.getLogger(__name__)

# Direct Reports columns that are measures; every other requested field is a key.
DIRECT_MEASURES = frozenset(
    {
        "Impressions",
        "Clicks",
        "Cost",
        "Ctr",
        "AvgCpc",
        "AvgCpm",
        "Conversions",
        "ConversionRate",
        "CostPerConversion",
        "Revenue",
        "GoalsRoi",
        "Profit",
        "Bounces",
        "BounceRate",
        "AvgPageviews",
        "Sessions",
        "AvgImpressionPosition",
        "AvgClickPosition",
        "AvgTrafficVolume",
        "AvgEffectiveBid",
        "WeightedImpressions",
        "WeightedCtr",
        "ImpressionReach",
        "AvgImpressionFrequency",
    }
)


def previous_range(start, end):
    days = (end - start).days + 1
    return start - timedelta(days=days), start - timedelta(days=1)


def as_number(value):
    if value in (None, "", "--"):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def failure(exc):
    """Tool-facing error: the API's reason, so the model can fix names and filters."""
    if isinstance(exc, IntegrationError):
        reason = exc.detail or exc.code
        status = "invalid" if exc.code.startswith(("http_400", "api_error")) else "unavailable"
        if "403" in exc.code:
            status, reason = "no_access", "Нет доступа к счётчику у токена Метрики бота."
        return {"status": status, "error": reason}
    logger.exception("Universal query failed")
    return {"status": "error", "error": "Источник не ответил или вернул неожиданный ответ."}


async def cached(checks, client, start, end, kind, options, load):
    digest = hashlib.sha256(json.dumps(options, sort_keys=True).encode()).hexdigest()[:8]
    key = SimpleNamespace(start=start, end=end)
    hit = await checks.repository.cached_analysis(client.id, key, kind + digest)
    if hit is not None:
        return hit
    result = safe_json(await load())
    await checks.repository.save_analysis(client.id, key, kind + digest, result, ttl_hours=6)
    return result


async def metrica_catalog(checks, client):
    counters = await checks.provider.client_counters(client)
    if not counters:
        return {
            "status": "no_counters",
            "counters": [],
            "limitations": ["В кампаниях клиента не указан счётчик Метрики."],
        }
    catalog = await checks.provider.metrica_catalog(client, counters)
    return {
        "status": "ok",
        "counters": catalog,
        "limitations": [
            f"Нет доступа к счётчикам {', '.join(str(c['id']) for c in catalog if c['access'] != 'ok')}: "
            "нужен гостевой доступ для логина токена Метрики."
        ]
        if any(c["access"] != "ok" for c in catalog)
        else [],
    }


async def metrica_query(checks, client, args):
    counters = await checks.provider.client_counters(client)
    counter_id = args.counter_id or (counters[0] if counters else None)
    if counter_id is None:
        return {"status": "no_counters", "error": "В кампаниях клиента не указан счётчик Метрики."}
    if counter_id not in counters:
        # The access boundary: a counter of another client is never queried.
        return {
            "status": "invalid",
            "error": "Этот счётчик не относится к клиенту. Возьмите ID из get_metrica_catalog.",
        }
    query = {
        "metrics": args.metrics,
        "dimensions": args.dimensions,
        "filters": args.filters,
        "sort": args.sort,
        "limit": args.limit,
    }
    start, end = args.date_range()

    async def run(first, last):
        return await cached(
            checks,
            client,
            first,
            last,
            "mq",
            {**query, "counter": counter_id},
            lambda: checks.provider.metrica_query(client, counter_id, first, last, **query),
        )

    try:
        current = await run(start, end)
        previous = await run(*previous_range(start, end)) if args.compare else None
    except Exception as exc:
        return failure(exc)
    result = {
        "status": "ok",
        "source": "Metrica Reporting API",
        "scope": "весь трафик счётчика, если фильтр не ограничивает источник или кампании",
        "counter_id": counter_id,
        "period": {"start": str(start), "end": str(end)},
        **current,
    }
    if previous is not None:
        first, last = previous_range(start, end)
        result["compare_period"] = {"start": str(first), "end": str(last)}
        before = {
            tuple(d["id"] or d["name"] for d in row["dimensions"]): row["metrics"]
            for row in previous["rows"]
        }
        for row in result["rows"]:
            old = before.get(tuple(d["id"] or d["name"] for d in row["dimensions"]), {})
            row["changes"] = {
                name: change(as_number(value), as_number(old.get(name)))
                for name, value in row["metrics"].items()
            }
        result["totals_changes"] = {
            name: change(as_number(value), as_number(previous["totals"].get(name)))
            for name, value in current["totals"].items()
        }
    limitations = []
    if current.get("sampled"):
        limitations.append(f"Данные семплированы (доля {current.get('sample_share')}).")
    if current.get("contains_sensitive_data"):
        limitations.append("Часть строк скрыта правилами обезличивания Метрики.")
    result["limitations"] = limitations
    return safe_json(result)


async def direct_query(checks, client, args):
    query = {
        "report_type": args.report_type,
        "fields": args.fields,
        "filters": [f.api() for f in args.filters],
        "goals": args.goal_ids,
        "order_by": [o.api() for o in args.order_by],
        "limit": args.limit,
    }
    start, end = args.date_range()

    async def run(first, last):
        return await cached(
            checks,
            client,
            first,
            last,
            "dq",
            query,
            lambda: checks.provider.direct_query(client, first, last, **query),
        )

    try:
        current = await run(start, end)
        previous = await run(*previous_range(start, end)) if args.compare else None
    except Exception as exc:
        return failure(exc)
    keys = [f for f in args.fields if f not in DIRECT_MEASURES]
    result = {
        "status": "ok",
        "source": "Direct Reports",
        "attribution": client.direct.attribution_model,
        "period": {"start": str(start), "end": str(end)},
        "columns": current["columns"],
        "rows": current["rows"],
        "truncated": len(current["rows"]) >= args.limit,
    }
    if previous is not None:
        first, last = previous_range(start, end)
        result["compare_period"] = {"start": str(first), "end": str(last)}
        before = {tuple(row.get(k) for k in keys): row for row in previous["rows"]}
        for row in result["rows"]:
            old = before.get(tuple(row.get(k) for k in keys), {})
            row["changes"] = {
                column: change(as_number(value), as_number(old.get(column)))
                for column, value in row.items()
                if column not in keys and as_number(value) is not None
            }
    result["limitations"] = (
        [f"Показаны первые {args.limit} строк; сузьте фильтр или сортировку."]
        if result["truncated"]
        else []
    )
    return safe_json(result)
