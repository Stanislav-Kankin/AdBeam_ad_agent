import json
from time import monotonic

from pydantic import ValidationError

from app.agent.schemas import (
    CampaignGoalArgs,
    CatalogArgs,
    ClientArgs,
    DirectQueryArgs,
    ListArgs,
    MetricaQueryArgs,
    MetricaReportArgs,
)
from app.analytics import queries
from app.analytics.diagnostics import drivers
from app.analytics.metrics import calculate
from app.analytics.rules import tracking_health
from app.analytics.trend import load_trend, quality_periods, summary
from app.domain.reports import CheckMode, TriggerSource
from app.storage.repository import safe_json

DESCRIPTIONS = {
    "list_clients": "Список доступных активных клиентов, ID, имён и алиасов. Не угадывай клиента; уточни неоднозначность.",
    "get_account_overview": "Единая стандартная проверка общего статуса: доступность, цели, бюджет, метрики, кампании, устройства, сигналы и рекомендации.",
    "get_campaign_breakdown": "Кампании и их вклад в изменение расходов и конверсий, рассчитанный backend.",
    "get_audience_breakdown": "Возраст, пол и уровень дохода рекламного трафика Директа; долгосрочные интересы аудитории сайта из Метрики.",
    "get_metrica_direct_report": "Отчёт Метрики по кампаниям Директа и выбранным целям: кампании, объявления, условия показа, поисковые фразы или площадки; включает поведение и сравнение периодов.",
    "get_campaign_goal_performance": "Кампании по целям, заданным в их настройках (ключевые цели и цель стратегии): конверсии, CPA и лучшая кампания по каждой цели. Не требует основных целей и Метрики; период до 366 дней без сравнения.",
    "get_half_year_trend": "Тренд за полгода по полным неделям: расход, конверсии по основным целям и CPA из Директа, отказы/глубина/время на сайте рекламного трафика из Метрики, готовый вывод. Те же числа, что в кнопке «Тренд за полгода». Вызывай первым на вопросы о динамике, росте, спаде, сезонности.",
    "get_metrica_catalog": "Справочник клиента: счётчики Метрики из его кампаний (название, сайт, есть ли доступ) и их цели с ID и названиями. Вызывай перед query_metrica и перед выбором целей.",
    "query_metrica": "Универсальный отчёт Метрики: сам выбери metrics, dimensions, filters, sort по вопросу пользователя. Период до 366 дней, compare=true добавляет прошлый период и изменения. Данные — весь трафик счётчика, если фильтр не ограничивает источник или кампании Директа.",
    "query_direct": "Универсальный отчёт Директа (Reports API): тип отчёта, поля-срезы и показатели, фильтры, цели для конверсий по каждой цели, сортировка. Период до 366 дней, compare=true — сравнение с прошлым периодом.",
}
# Device, region, query and placement slices are query_direct now.
DIMENSIONS = {"get_campaign_breakdown": "campaign"}

SCHEMAS = {
    "list_clients": ListArgs,
    "get_metrica_direct_report": MetricaReportArgs,
    "get_campaign_goal_performance": CampaignGoalArgs,
    "get_metrica_catalog": CatalogArgs,
    "get_half_year_trend": CatalogArgs,
    "query_metrica": MetricaQueryArgs,
    "query_direct": DirectQueryArgs,
}


def tool_schemas():
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": (SCHEMAS.get(name, ClientArgs)).model_json_schema(),
            },
        }
        for name, description in DESCRIPTIONS.items()
    ]


class ToolRegistry:
    def __init__(self, checks):
        self.checks = checks

    async def execute(self, name, arguments, *, chat_id, request_id, user_id=None):
        start, status, error, validated = monotonic(), "ok", None, {}
        try:
            if chat_id not in self.checks.registry.allowed_chats:
                raise PermissionError
            if name not in DESCRIPTIONS:
                raise ValueError("unknown_tool")
            schema = SCHEMAS.get(name, ClientArgs)
            args = schema.model_validate_json(arguments)
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
            matches = self.checks.registry.resolve(chat_id, args.client_id)
            if len(matches) != 1:
                raise PermissionError
            client = await self.checks.ensure_goals(matches[0])
            if name == "get_half_year_trend":
                trend = await load_trend(self.checks, client)
                return {
                    "client_id": client.id,
                    "source": "Direct Reports (основные цели); Metrica (рекламный трафик)",
                    "period": {"start": str(trend["start"]), "end": str(trend["end"])},
                    "weeks": [
                        {
                            "week_start": str(w["start"]),
                            **calculate(w["totals"]).model_dump(
                                mode="json", include={"spend", "clicks", "conversions", "cpa"}
                            ),
                        }
                        for w in trend["weeks"]
                    ],
                    "quality_last4_vs_prev4": quality_periods(trend),
                    "reading": summary(trend),
                }
            if name == "get_metrica_catalog":
                return {
                    "client_id": client.id,
                    **await queries.metrica_catalog(self.checks, client),
                }
            if name == "query_metrica":
                result = await queries.metrica_query(self.checks, client, args)
                status = result.get("status", status)
                return {"client_id": client.id, **result}
            if name == "query_direct":
                result = await queries.direct_query(self.checks, client, args)
                status = result.get("status", status)
                return {"client_id": client.id, **result}
            if name == "get_campaign_goal_performance":
                first_day, last_day = args.date_range()
                return {
                    "client_id": client.id,
                    "mock": self.checks.provider.mock,
                    **await self.checks.campaign_goal_performance(
                        client,
                        first_day,
                        last_day,
                        top_n=args.top_n,
                        segment=args.segment,
                        campaign_ids=args.campaign_ids,
                    ),
                }
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
                    [client.id],
                    period,
                    CheckMode.STANDARD,
                    TriggerSource.AGENT,
                    chat_id=chat_id,
                    user_id=user_id,
                )
                return {**base, "reports": [r.model_dump(mode="json") for r in reports]}
            if name == "get_metrica_direct_report":
                resolution = await self.checks.resolve_campaign_references(
                    client, period, args.campaign_ids
                )
                if resolution["unresolved"] or resolution["ambiguous"]:
                    status = "invalid"
                    error = (
                        "Кампанию не удалось определить однозначно. "
                        "Вызовите get_campaign_breakdown и повторите запрос с ID "
                        "или точным названием."
                    )
                    return {
                        "status": status,
                        "error": error,
                        "unresolved": resolution["unresolved"],
                        "ambiguous": resolution["ambiguous"],
                    }
                return {
                    **base,
                    "metrica_report": await self.checks.metrica_report(
                        client,
                        period,
                        args.report,
                        goal_ids=args.goal_ids,
                        campaign_ids=resolution["ids"],
                        top_n=args.top_n,
                    ),
                }
            if name == "get_audience_breakdown":
                return {**base, "audience": await self.checks.audience(client, period)}
            if name in DIMENSIONS:
                # Check tracking first so unavailable goals are never presented as valid CPA/CR.
                now, before = await self.checks.snapshots(client, period)
                health = tracking_health(client, now, before, period)
                dim = DIMENSIONS[name]
                a, b = (
                    (now.direct, before.direct)
                    if dim == "campaign"
                    else (
                        await self.checks.breakdown(client, period.current, dim),
                        await self.checks.breakdown(client, period.previous, dim),
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
            raise ValueError("unknown_tool")
        except PermissionError:
            status, error = "denied", "Клиент не найден или недоступен этому чату."
        except ValidationError as exc:
            fields = sorted(
                {
                    ".".join(str(part) for part in item["loc"])
                    for item in exc.errors(include_input=False, include_url=False)
                }
            )
            status, error = (
                "invalid",
                "Некорректные параметры: "
                + (", ".join(fields) if fields else "неизвестное поле")
                + ". Используйте ID/логин из list_clients, period 1d–90d или четыре ISO-даты.",
            )
        except (ValueError, TypeError):
            status, error = (
                "invalid",
                "Период или параметры несопоставимы. Используйте завершённые равные периоды до 90 дней.",
            )
        except Exception:
            status, error = "error", "Инструмент недоступен. Данные не получены."
        finally:
            await self.checks.repository.tool_event(
                request_id=request_id,
                chat_id=str(chat_id),
                user_id=str(user_id) if user_id is not None else None,
                tool=name if name in DESCRIPTIONS else "unknown",
                client_id=validated.get("client_id"),
                arguments=validated,
                status=status,
                error=error,
                duration_seconds=monotonic() - start,
            )
        return {"status": status, "error": error}

    async def call(self, *args, **kwargs):
        result = safe_json(await self.execute(*args, **kwargs))
        # Bound valid structured JSON, never cut serialized JSON halfway through a field.
        for _ in range(12):
            # Every tool result is resent on each later agent step, so keep it compact.
            if len(json.dumps(result, ensure_ascii=False)) <= 20000:
                return result

            def shrink(value):
                if isinstance(value, dict):
                    for key, child in value.items():
                        if (
                            isinstance(child, list)
                            and len(child) > 1
                            and key not in ("main_goal_ids", "missing_goal_ids")
                        ):
                            value[key] = child[: max(1, len(child) // 2)]
                        else:
                            shrink(child)
                elif isinstance(value, list):
                    for child in value:
                        shrink(child)

            shrink(result)
            result["output_limited"] = True
        return {"status": "output_limit", "error": "Ответ слишком велик. Сузьте период или top_n."}
