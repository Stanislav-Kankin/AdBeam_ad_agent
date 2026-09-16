from time import monotonic

from pydantic import ValidationError

from app.agent.schemas import ClientArgs, ListArgs
from app.analytics.diagnostics import drivers, snapshot_metrics
from app.analytics.metrics import calculate, compare
from app.analytics.rules import tracking_health
from app.domain.reports import CheckMode, TriggerSource
from app.storage.repository import safe_json

DESCRIPTIONS = {
    "list_clients": "Список доступных активных клиентов, ID, имён и алиасов. Не угадывай клиента; уточни неоднозначность.",
    "get_account_overview": "Единая стандартная проверка общего статуса: доступность, цели, бюджет, метрики, кампании, устройства, сигналы и рекомендации.",
    "compare_periods": "Сравнение завершённых равных периодов: абсолютные значения и проценты, объём данных.",
    "get_campaign_breakdown": "Кампании и их вклад в изменение расходов и конверсий, рассчитанный backend.",
    "get_device_breakdown": "Агрегированные показатели по устройствам.",
    "get_geo_breakdown": "Агрегированные показатели по регионам (ID регионов Директа).",
    "get_search_queries": "Топ поисковых запросов по расходу, клики и основные конверсии; контакты маскируются.",
    "get_placements": "Топ площадок РСЯ по расходу, кликам и основным конверсиям.",
    "get_metrica_goals": "Доступные цели и достижения основных целей Метрики, без персональных данных.",
    "check_tracking_health": "Проверка поступления данных, наличия целей и исчезновения конверсий. Не является тестом форм на сайте.",
    "get_revenue": "Выручка из настроенного источника, её статус, период и сопоставимость.",
    "get_drr": "ДРР, рассчитанный backend; при отсутствующей, нулевой или несопоставимой выручке — причина отказа.",
}
DIMENSIONS = {
    "get_campaign_breakdown": "campaign",
    "get_device_breakdown": "device",
    "get_geo_breakdown": "geo",
    "get_search_queries": "search",
    "get_placements": "placement",
}


def tool_schemas():
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": (
                    ListArgs if name == "list_clients" else ClientArgs
                ).model_json_schema(),
            },
        }
        for name, description in DESCRIPTIONS.items()
    ]


class ToolRegistry:
    def __init__(self, checks):
        self.checks = checks

    async def execute(self, name, arguments, *, chat_id, request_id):
        start, status, error, validated = monotonic(), "ok", None, {}
        try:
            if chat_id not in self.checks.registry.allowed_chats:
                raise PermissionError
            if name not in DESCRIPTIONS:
                raise ValueError("unknown_tool")
            args = (ListArgs if name == "list_clients" else ClientArgs).model_validate_json(
                arguments
            )
            validated = args.model_dump(mode="json")
            if name == "list_clients":
                clients = self.checks.registry.visible(chat_id)
                return {
                    "clients": [
                        {"id": c.id, "name": c.name, "aliases": c.aliases}
                        for c in clients[args.offset : args.offset + args.top_n]
                    ],
                    "total": len(clients),
                    "next_offset": args.offset + args.top_n
                    if len(clients) > args.offset + args.top_n
                    else None,
                }
            client = self.checks.registry.require(chat_id, args.client_id)
            period = args.analysis_period()
            base = {
                "client_id": client.id,
                "period": period.model_dump(mode="json"),
                "mock": self.checks.provider.mock,
                "source": "Direct Reports; Metrica",
                "main_goal_ids": client.direct.main_goal_ids,
            }
            if name == "get_account_overview":
                reports, _ = await self.checks.run_check(
                    [client.id], period, CheckMode.STANDARD, TriggerSource.AGENT, chat_id=chat_id
                )
                return {**base, "reports": [r.model_dump(mode="json") for r in reports]}
            if name in DIMENSIONS:
                # Check tracking first so unavailable goals are never presented as valid CPA/CR.
                now, before = await self.checks.snapshots(client, period)
                health = tracking_health(client, now, before, period)
                dim = DIMENSIONS[name]
                a, b = (
                    (now.direct, before.direct)
                    if dim == "campaign"
                    else (
                        await self.checks.provider.breakdown(client, period.current, dim),
                        await self.checks.provider.breakdown(client, period.previous, dim),
                    )
                )
                rows = []
                for row in sorted(a.rows, key=lambda r: r.totals.spend or 0, reverse=True)[
                    : args.top_n
                ]:
                    totals = row.totals.model_copy()
                    if not health["healthy"]:
                        totals.conversions = None
                    rows.append(
                        {
                            "id": row.id,
                            "name": row.name,
                            "metrics": calculate(totals).model_dump(mode="json"),
                        }
                    )
                changes = drivers(a, b)[: args.top_n]
                if not health["healthy"]:
                    changes = [
                        {
                            "id": r["id"],
                            "name": r["name"],
                            "spend_delta": r["spend_delta"],
                            "conversions_delta": None,
                        }
                        for r in changes
                    ]
                return {
                    **base,
                    "status": a.status,
                    "rows": rows,
                    "drivers": changes,
                    "total_rows": len(a.rows),
                    "truncated": len(a.rows) > args.top_n,
                    "tracking": health,
                    "limitations": a.limitations + b.limitations,
                }
            now, before = await self.checks.snapshots(client, period)
            health = tracking_health(client, now, before, period)
            if name == "check_tracking_health":
                return {**base, **health}
            if name == "get_metrica_goals":
                goals = sorted(now.metrica.goals, key=lambda g: not g["primary"])
                return {
                    **base,
                    "status": now.metrica.status,
                    "goals": goals[: args.top_n],
                    "total_rows": len(goals),
                    "truncated": len(goals) > args.top_n,
                    "missing_goal_ids": now.metrica.missing_goal_ids,
                }
            if name == "get_revenue":
                return {**base, **now.revenue.model_dump(mode="json")}
            a, b = snapshot_metrics(now, healthy=health["healthy"]), snapshot_metrics(before)
            if name == "get_drr":
                return {
                    **base,
                    "drr": a.drr,
                    "spend": a.spend,
                    "revenue": a.revenue,
                    "status": "ok" if a.drr is not None else "not_calculated",
                    "reason": ""
                    if a.drr is not None
                    else now.revenue.reason or "Выручка отсутствует, равна нулю или несопоставима.",
                }
            return {
                **base,
                "metrics": compare(a, b),
                "tracking": health,
                "sufficient_data": health["healthy"]
                and (a.clicks or 0) >= client.targets.minimum_clicks,
            }
        except PermissionError:
            status, error = "denied", "Клиент не найден или недоступен этому чату."
        except (ValidationError, ValueError, TypeError):
            status, error = (
                "invalid",
                "Неверный инструмент или параметры. Проверь ID, даты (до 90 завершённых дней) и top_n (1–50).",
            )
        except Exception:
            status, error = "error", "Инструмент недоступен. Данные не получены."
        finally:
            await self.checks.repository.tool_event(
                request_id=request_id,
                chat_id=str(chat_id),
                tool=name if name in DESCRIPTIONS else "unknown",
                client_id=validated.get("client_id"),
                arguments=validated,
                status=status,
                error=error,
                duration_seconds=monotonic() - start,
            )
        return {"status": status, "error": error}

    async def call(self, *args, **kwargs):
        return safe_json(await self.execute(*args, **kwargs))
