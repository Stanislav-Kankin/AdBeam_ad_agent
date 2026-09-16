from decimal import Decimal

from app.domain.reports import ClientReport
from app.security import redact

ICONS = {"green": "🟢", "yellow": "🟡", "red": "🔴", "unknown": "⚪"}
METRIC_NAMES = {
    "spend": "Расход, ₽ (без НДС)",
    "impressions": "Показы",
    "clicks": "Клики",
    "ctr": "CTR, %",
    "cpc": "CPC, ₽",
    "conversions": "Основные конверсии",
    "cr": "CR из клика, %",
    "cpa": "CPA, ₽",
    "revenue": "Выручка, ₽",
    "drr": "ДРР, %",
}
STATUS_NAMES = {
    "ok": "данные получены",
    "unavailable": "источник недоступен",
    "no_data": "нет данных",
    "insufficient": "данных недостаточно",
    "not_checked": "не выполнялась",
    "no_problems_detected": "в выполненных проверках сигналов не обнаружено",
    "signals_detected": "обнаружены сигналы",
}


def fmt(value):
    if value is None:
        return "не рассчитано"
    return f"{Decimal(value):,.2f}".replace(",", " ").replace(".", ",")


def detailed(report: ClientReport) -> str:
    lines = [
        "🧪 MOCK — тестовые данные" if report.mock else "📊 AdBeam Performance Analyst",
        report.client_name,
        f"Период: {report.period.current.label()} (МСК)",
        f"Сравнение: {report.period.previous.label()}",
        f"Общий статус: {ICONS.get(report.level, '⚪')} {STATUS_NAMES.get(report.status, report.status)}",
        "",
        "Ключевые показатели:",
    ]
    for key, title in METRIC_NAMES.items():
        diff = report.changes[key]["percent"]
        suffix = (
            f"; изменение {'+' if Decimal(diff) > 0 else ''}{fmt(diff)}%"
            if diff is not None
            else "; изменение не рассчитано (нет базы сравнения)"
        )
        if key in ("ctr", "cr", "drr"):
            absolute = report.changes[key]["absolute"]
            if absolute is not None:
                suffix = (
                    f"; изменение {'+' if Decimal(absolute) > 0 else ''}{fmt(absolute)} п.п."
                    + suffix.replace("; изменение", "; относительно прошлого периода", 1)
                )
        lines.append(
            f"{title}: сейчас {fmt(getattr(report.current, key))}; раньше {fmt(getattr(report.previous, key))}{suffix}"
        )
    if report.goal_metrics and not report.mock:
        lines += [
            "",
            "Цели Метрики: достижения за указанные периоды, не уникальные заявки",
        ]
        for goal in report.goal_metrics:
            lines.append(
                f"• [{goal.get('counter_id', '')}/{goal['id']}] {goal['name']}: "
                f"сейчас {fmt(goal['reaches'])}; раньше {fmt(goal.get('previous_reaches'))}"
            )
    lines += ["", "Что изменилось — три главных вывода:"]
    lines += [f"• {s.message}" for s in report.signals[:3]] or [
        "• Существенных сигналов в выполненных проверках не обнаружено."
        if report.level == "green"
        else "• Для вывода недостаточно данных или объёма проверки."
    ]
    lines += [
        "",
        "Вероятные причины (гипотезы):",
        "Причинность по агрегатам не доказана. Сигналы требуют проверки источников, состава трафика и изменений на сайте.",
        "",
        "Подтверждающие данные (факты и расчёты):",
    ]
    lines += [f"• {s.message} {s.evidence}" for s in report.signals[:6]]
    for row in report.drivers[:3]:
        lines.append(
            f"• {row['name']}: вклад в изменение расхода {fmt(row['spend_delta'])} ₽; конверсий {fmt(row['conversions_delta'])}."
        )
    lines += ["", "Что рекомендуется проверить:"]
    lines += [f"• {v}" for v in dict.fromkeys(s.next_check for s in report.signals[:5])] or [
        "• Продолжить наблюдение и сверить цели с бизнес-задачей клиента."
    ]
    lines += [
        "",
        "Ограничения анализа:",
        *[f"• {v}" for v in report.limitations],
        "• CR относится к кликам. Сумма целей не равна числу уникальных заказов/лидов.",
        "",
        "Источники данных:",
    ]
    lines += [
        f"• {key}: {STATUS_NAMES.get(value, value)}" for key, value in report.source_status.items()
    ]
    lines += [
        f"Основные цели: {', '.join(report.main_goal_ids) or 'не настроены'}",
        "Проверки: "
        + "; ".join(
            f"{key}: {STATUS_NAMES.get(value, value)}" for key, value in report.checks.items()
        ),
    ]
    return redact("\n".join(lines))


def compact(
    reports: list[ClientReport], period, errors: list[str] | None = None, summary=False
) -> str:
    lines = [
        "🧪 MOCK — тестовые данные"
        if any(r.mock for r in reports)
        else "📊 AdBeam Performance Analyst",
        f"Сводка: {period.current.label()} (МСК)",
        f"Сравнение: {period.previous.label()}",
        f"Проверено: {len(reports)} проектов",
        "",
    ]
    healthy = 0
    for report in sorted(
        reports, key=lambda r: {"red": 0, "yellow": 1, "unknown": 2, "green": 3}.get(r.level, 2)
    ):
        if report.level == "green" and not summary:
            healthy += 1
            continue
        lines.append(f"{ICONS.get(report.level, '⚪')} {report.client_name}")
        for key in (
            "spend",
            "impressions",
            "clicks",
            "ctr",
            "cpc",
            "conversions",
            "cr",
            "cpa",
            "revenue",
            "drr",
        ):
            lines.append(f"{METRIC_NAMES[key]}: {fmt(getattr(report.current, key))}")
        lines += [s.message for s in report.signals[:2]]
        unavailable = [
            key for key, value in report.source_status.items() if value not in ("ok", "not_checked")
        ]
        if unavailable:
            lines.append("Ограничения данных: " + ", ".join(unavailable))
            lines.extend(report.limitations[:2])
        elif report.limitations:
            lines.append(report.limitations[0])
        if not summary and report.drivers:
            driver = report.drivers[0]
            lines.append(
                f"Наибольшее изменение расхода: {driver['name']} ({fmt(driver['spend_delta'])} ₽)."
            )
        lines.append("")
    if healthy:
        lines.append(f"🟢 Без существенных сигналов в выполненных проверках: {healthy} проектов.")
    if errors:
        lines += [f"⚠ {error}" for error in errors]
    lines += [
        "Для деталей: /check <клиент>. Источники: Директ, Метрика; выручка — по настройкам клиента."
    ]
    return redact("\n".join(lines))


def split_message(text: str, limit=4000) -> list[str]:
    """Plain text; preserve all characters and Telegram UTF-16 code unit limits."""
    parts = []
    while text:
        size, cut = 0, 0
        for char in text:
            width = 2 if ord(char) > 0xFFFF else 1
            if size + width > limit:
                break
            size, cut = size + width, cut + 1
        if cut == len(text):
            parts.append(text)
            break
        newline = text.rfind("\n", 0, cut)
        if newline > cut // 2:
            cut = newline + 1
        parts.append(text[:cut])
        text = text[cut:]
    return parts
