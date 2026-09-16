from decimal import Decimal

from app.analytics.metrics import change, expected_budget
from app.analytics.periods import today_moscow
from app.domain.reports import DataStatus, Metrics, Signal, Snapshot


def tracking_health(client, current: Snapshot, previous: Snapshot, period) -> dict:
    reasons = []
    if current.direct.status != DataStatus.OK:
        reasons.append("Данные Директа недоступны, отсутствуют или неполные.")
    if current.metrica.status != DataStatus.OK:
        reasons.append("Данные Метрики недоступны, отсутствуют или неполные.")
    if not client.metrica.main_goal_ids or current.metrica.missing_goal_ids:
        reasons.append("Основные цели не настроены или отсутствуют в счётчике.")
    if client.direct.main_goal_ids and current.direct.totals.conversions is None:
        reasons.append("Директ не вернул достоверную статистику основных конверсий.")
    if current.metrica.sampled:
        reasons.append("Метрика вернула выборочные данные.")
    if current.direct.period != period.current or current.metrica.period != period.current:
        reasons.append("Период источника не совпадает с запросом.")
    previous_conversions = previous.direct.totals.conversions
    goal_values = [
        Decimal(g["reaches"])
        for g in current.metrica.goals
        if g["primary"] and g["reaches"] is not None
    ]
    if (
        previous_conversions is not None
        and previous_conversions >= client.targets.minimum_conversions
        and current.direct.totals.conversions == 0
        and goal_values
        and sum(goal_values) == 0
        and (current.direct.totals.clicks or 0) >= client.targets.minimum_clicks
    ):
        reasons.append(
            "Конверсии исчезли одновременно в Директе и Метрике; необходимо проверить аналитику."
        )
    return {
        "status": "no_problems_detected" if not reasons else "needs_verification",
        "healthy": not reasons,
        "reasons": reasons,
        "direct": current.direct.status,
        "metrica": current.metrica.status,
        "main_goal_ids": client.metrica.main_goal_ids,
        "visits": current.metrica.visits,
        "has_conversions": current.direct.totals.conversions is not None
        and current.direct.totals.conversions > 0,
        "period": period.model_dump(mode="json"),
        "limitation": "Это проверка поступления данных, а не тест формы/событий на сайте.",
    }


def evaluate(
    client,
    current: Metrics,
    previous: Metrics,
    period,
    tracking: dict,
    snapshot: Snapshot,
    *,
    today=None,
) -> list[Signal]:
    targets = client.targets
    signals = []

    def add(type_, level, message, actual, evidence, next_check, sufficient=True):
        signals.append(
            Signal(
                type=type_,
                level=level,
                message=message,
                actual=actual,
                period=period,
                evidence=evidence,
                confidence="high" if sufficient else "low",
                sufficient_data=sufficient,
                next_check=next_check,
            )
        )

    if not tracking["healthy"]:
        add(
            "tracking",
            "yellow",
            "Требуется проверка аналитики.",
            {"reasons": tracking["reasons"]},
            " ".join(tracking["reasons"]),
            "Проверить доступы, счётчик, цели и поступление событий.",
            False,
        )
    enough = current.spend is not None and current.spend >= targets.minimum_spend_for_analysis
    traffic = (current.clicks or 0) >= targets.minimum_clicks
    mature = ((today or today_moscow()) - period.current.end).days > targets.conversion_delay_days
    conversion_ready = enough and traffic and mature and tracking["healthy"]
    threshold = targets.minimum_spend_for_analysis
    if targets.target_cpa:
        threshold = min(
            threshold, targets.target_cpa * Decimal(str(targets.no_conversion_cpa_multiple))
        )
    if (
        current.spend is not None
        and current.spend >= threshold
        and traffic
        and mature
        and tracking["healthy"]
        and current.conversions == 0
    ):
        add(
            "spend_without_conversions",
            "red",
            "Расход без основных конверсий.",
            {"spend": current.spend, "threshold": threshold, "conversions": 0},
            "Есть достаточный трафик, порог расхода превышен, основных конверсий нет.",
            "Проверить цели и качество трафика по кампаниям.",
        )
    if conversion_ready and targets.target_cpa and current.cpa is not None:
        excess = (current.cpa / targets.target_cpa - 1) * 100
        if excess >= Decimal(str(targets.cpa_excess_percent)):
            add(
                "cpa_high",
                "red",
                "CPA превышает целевой уровень.",
                {"cpa": current.cpa, "target_cpa": targets.target_cpa, "excess_percent": excess},
                "CPA рассчитан по основным целям; превышен настроенный порог.",
                "Проверить кампании с максимальным вкладом в расход и падение конверсий.",
            )
    cpc_delta = change(current.cpc, previous.cpc)["percent"]
    if (
        enough
        and traffic
        and (previous.clicks or 0) >= targets.minimum_clicks
        and cpc_delta is not None
        and abs(cpc_delta) >= targets.cpc_change_percent
    ):
        add(
            "cpc_change",
            "yellow",
            "Заметно изменился CPC.",
            {"current": current.cpc, "previous": previous.cpc, "percent": cpc_delta},
            "Сравнены CPC двух завершённых периодов одинаковой длины.",
            "Проверить аукцион, устройства, запросы и распределение расхода.",
        )
    cr_delta = change(current.cr, previous.cr)["percent"]
    if (
        conversion_ready
        and (previous.conversions or 0) >= targets.minimum_conversions
        and cr_delta is not None
        and cr_delta <= -targets.cr_drop_percent
    ):
        add(
            "cr_drop",
            "yellow",
            "Снизилась конверсия из клика.",
            {"current": current.cr, "previous": previous.cr, "percent": cr_delta},
            "CR = основные конверсии Директа / клики × 100.",
            "Проверить основные цели и разрез устройств.",
        )
    budget = expected_budget(period.current, targets)
    if budget and current.spend is not None:
        deviation = (current.spend / budget - 1) * 100
        if abs(deviation) >= targets.budget_deviation_percent:
            add(
                "budget_pacing",
                "yellow",
                "Расход отклоняется от равномерного плана.",
                {"spend": current.spend, "expected": budget, "deviation_percent": deviation},
                "План рассчитан пропорционально календарным дням периода.",
                "Проверить график расхода, остановки и согласованный медиаплан.",
            )
    spend_delta = change(current.spend, previous.spend)["percent"]
    if enough and spend_delta is not None and abs(spend_delta) >= targets.spend_change_percent:
        add(
            "spend_change",
            "yellow",
            "Заметно изменился расход.",
            {"current": current.spend, "previous": previous.spend, "percent": spend_delta},
            "Сравнены расходы двух завершённых периодов.",
            "Проверить вклад кампаний и план бюджета.",
        )
    if targets.target_drr and current.drr is not None and current.drr > targets.target_drr:
        add(
            "drr_high",
            "yellow",
            "ДРР выше целевого уровня.",
            {"drr": current.drr, "target_drr": targets.target_drr},
            "ДРР = расход без НДС / сопоставимая выручка × 100.",
            "Проверить атрибуцию выручки, возвраты и конверсионную задержку.",
        )
    if snapshot.direct.campaigns_status == DataStatus.OK:
        campaigns = snapshot.direct.campaigns
        stopped = [
            str(c["Id"])
            for c in campaigns
            if c.get("State") in {"OFF", "SUSPENDED"}
            or c.get("Status") == "REJECTED"
            or c.get("StatusPayment") == "DISALLOWED"
        ]
        active = [c for c in campaigns if c.get("State") == "ON"]
        if not active:
            add(
                "no_active_campaigns",
                "yellow",
                "Нет активных кампаний.",
                {"active": 0},
                "Состояние кампаний получено из Direct API на момент проверки.",
                "Уточнить, запланирована ли остановка рекламы.",
            )
        if stopped:
            add(
                "campaign_states",
                "yellow",
                "Есть остановленные, отклонённые или неоплаченные кампании.",
                {"campaign_ids": stopped[:20], "count": len(stopped)},
                "Статусы Direct API на момент проверки.",
                "Проверить причины остановок; они могут быть запланированы.",
            )
    return signals
