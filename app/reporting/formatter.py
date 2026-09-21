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


def fmt_short(value, *, money=False):
    if value is None:
        return "—"
    number = Decimal(value)
    decimals = 2 if money or number != number.to_integral() else 0
    return f"{number:,.{decimals}f}".replace(",", " ").replace(".", ",")


def delta_short(report, key):
    value = report.changes.get(key, {}).get("percent")
    if value is None:
        return ""
    value = Decimal(value)
    return f" ({'+' if value > 0 else ''}{fmt_short(value, money=True)}%)"


def has_signal(report, type_):
    return any(signal.type == type_ for signal in report.signals)


def daily_digest(results):
    """One operational digest for yesterday and the last seven completed days."""
    sections = []
    all_errors = []
    for title, reports, period, errors in results:
        active = [report for report in reports if report.current.spend is not None]
        inactive = [
            report
            for report in reports
            if report.source_status.get("Директ") == "no_data"
            and has_signal(report, "no_active_campaigns")
        ]
        broken = [
            report for report in reports if report.current.spend is None and report not in inactive
        ]
        sections.append((title, reports, period, active, inactive, broken))
        all_errors.extend(errors or [])

    lines = ["📊 Ежедневный контроль рекламы"]
    for title, _reports, period, active, inactive, broken in sections:
        lines.append(
            f"{title} · {period.current.label()}: работала у {len(active)}, "
            f"без активности {len(inactive)}, проблема данных {len(broken)}."
        )

    weekly = sections[-1]
    _, reports, period, active, inactive, broken = weekly
    meaningful = {
        "cpa_high",
        "cr_drop",
        "cpc_change",
        "budget_pacing",
        "spend_change",
        "drr_high",
        "campaign_without_conversions",
        "device_cr_drop",
    }
    attention = [
        report
        for report in active
        if any(signal.type in meaningful for signal in report.signals)
        or has_signal(report, "campaign_states")
    ]
    attention.sort(
        key=lambda report: (
            0 if report.level == "red" else 1,
            -abs(Decimal(report.changes["spend"]["percent"] or 0)),
            -(report.current.spend or 0),
        )
    )
    lines += ["", f"Главное за 7 дней · сравнение с {period.previous.label()}:"]
    if attention:
        for report in attention[:5]:
            facts = [f"расход {fmt_short(report.current.spend)} ₽{delta_short(report, 'spend')}"]
            if report.current.cpc is not None:
                facts.append(
                    f"CPC {fmt_short(report.current.cpc, money=True)} ₽{delta_short(report, 'cpc')}"
                )
            signal = next(
                (signal.message for signal in report.signals if signal.type in meaningful),
                "есть кампании не в активном состоянии",
            )
            lines.append(
                f"{ICONS.get(report.level, '🟡')} {report.client_name}: "
                + "; ".join(facts)
                + f". {signal}"
            )
    elif active:
        lines.append("🟢 Существенных изменений расхода и стоимости клика не обнаружено.")
    else:
        lines.append("Нет проектов с расходом и достаточными данными для сравнения.")

    stable = [report for report in active if report not in attention]
    if stable:
        names = ", ".join(report.client_name for report in stable[:6])
        suffix = f" и ещё {len(stable) - 6}" if len(stable) > 6 else ""
        lines += ["", f"Без существенных сигналов: {names}{suffix}."]
    if inactive:
        names = ", ".join(report.client_name for report in inactive[:6])
        suffix = f" и ещё {len(inactive) - 6}" if len(inactive) > 6 else ""
        lines += ["", f"Без рекламной активности: {names}{suffix}."]
    if broken:
        names = ", ".join(report.client_name for report in broken[:6])
        suffix = f" и ещё {len(broken) - 6}" if len(broken) > 6 else ""
        lines += ["", f"Не удалось получить расход: {names}{suffix}."]

    metrica_issues = [
        report
        for report in reports
        if report.source_status.get("Метрика") not in ("ok", "not_checked")
    ]
    missing_goals = [report for report in reports if not report.main_goal_ids]
    actions = []
    if attention:
        actions.append(
            "Разобрать вклад кампаний у: " + ", ".join(r.client_name for r in attention[:3]) + "."
        )
    if metrica_issues:
        actions.append(f"Проверить доступ или настройку Метрики у {len(metrica_issues)} проектов.")
    if missing_goals:
        actions.append(
            f"Выбрать основные цели у {len(missing_goals)} проектов для расчёта CPA и CR."
        )
    if actions:
        lines += [
            "",
            "Что сделать сегодня:",
            *[f"{i}. {text}" for i, text in enumerate(actions[:3], 1)],
        ]
    if all_errors:
        lines.append(f"Технически не завершены проверки: {len(set(all_errors))}.")
    lines += ["", "Подробности: /check <клиент> 7d"]
    return redact("\n".join(lines))


def detailed(report: ClientReport) -> str:
    lines = [
        "🧪 MOCK — тестовые данные" if report.mock else "📊 AdBeam Performance Analyst",
        report.client_name,
        f"Сейчас: {report.period.current.label()} (МСК)",
        f"Раньше: {report.period.previous.label()}",
        "",
    ]
    direct = report.source_status.get("Директ")
    if direct == "no_data":
        lines += [
            "⚪ Эффективность рекламы за период оценить нельзя.",
            "Директ ответил, но не вернул строк статистики за выбранный период.",
        ]
    elif report.current.spend is None:
        lines += [
            "🟡 Эффективность рекламы пока оценить нельзя.",
            "Данные о расходе не получены. Это не означает нулевой расход.",
        ]
    else:
        lines += [
            f"{ICONS.get(report.level, '⚪')} "
            + (
                "Найдены изменения, требующие внимания."
                if report.level == "red"
                else "Анализ ограничен: часть показателей нельзя оценить."
                if report.level != "green"
                else "Существенных отклонений в выполненных проверках не найдено."
            )
        ]
    available = any(
        getattr(report.current, k) is not None or getattr(report.previous, k) is not None
        for k in METRIC_NAMES
    )
    if available:
        lines += ["", "Ключевые показатели:"]
    for key, title in METRIC_NAMES.items():
        if getattr(report.current, key) is None and getattr(report.previous, key) is None:
            continue
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
    useful = [s for s in report.signals if s.type != "tracking"]
    if useful:
        lines += ["", "Что известно:"]
        for signal in useful[:3]:
            lines.append(f"• {signal.message}")
            if signal.evidence:
                lines.append(signal.evidence)
    if report.metrica_current and any(
        value is not None for value in report.metrica_current.values()
    ):
        labels = {
            "visits": "Визиты сайта",
            "users": "Посетители сайта",
            "pageviews": "Просмотры страниц",
            "bounce_rate": "Отказы, %",
            "page_depth": "Глубина просмотра",
            "avg_visit_duration_seconds": "Среднее время на сайте, сек.",
        }
        lines += ["", "Поведение на сайте (весь выбранный счётчик):"]
        for key, label in labels.items():
            current = report.metrica_current.get(key)
            previous = report.metrica_previous.get(key)
            if current is not None:
                lines.append(f"{label}: сейчас {fmt(current)}; раньше {fmt(previous)}")
    if report.goal_metrics and not report.mock:
        goals = report.goal_metrics
        active = [
            g
            for g in goals
            if any(
                g.get(k) is not None and Decimal(str(g[k])) > 0
                for k in ("reaches", "previous_reaches")
            )
        ]
        zeros = sum(
            all(
                g.get(k) is not None and Decimal(str(g[k])) == 0
                for k in ("reaches", "previous_reaches")
            )
            for g in goals
        )
        lines += [
            "",
            "Цели Метрики:",
            f"Получены данные по {len(goals)} целям. По {zeros} — ноль достижений в обоих периодах.",
            (
                "Это все достижения на выбранном счётчике, включая другие источники трафика."
                if report.goal_scope == "counter"
                else "Это события, отнесённые к кампаниям этого клиента, а не все обращения на сайте."
            ),
        ]
        for goal in sorted(
            active,
            key=lambda g: max(
                Decimal(str(g.get("reaches") or 0)), Decimal(str(g.get("previous_reaches") or 0))
            ),
            reverse=True,
        )[:5]:
            lines.append(
                f"• {goal['name']}: сейчас {fmt(goal.get('reaches'))}; раньше {fmt(goal.get('previous_reaches'))}"
            )
        if len(active) > 5:
            lines.append(
                f"Показаны 5 из {len(active)} целей с достижениями. Все полученные цели сохранены в результате проверки."
            )
    blockers = []
    for signal in report.signals:
        if signal.type == "tracking":
            blockers.extend(
                reason
                for reason in signal.actual.get("reasons", [])
                if not reason.startswith(
                    ("Данные Директа", "Данные Метрики", "Директ не вернул строк", "Основные цели")
                )
            )
    if not report.main_goal_ids:
        blockers.append(
            "Не выбраны основные цели: общие конверсии и стоимость заявки (CPA) не определены. Суммировать все действия на сайте как заявки нельзя."
        )
    if report.current.revenue is None:
        blockers.append("Выручка не получена — окупаемость рекламы оценить нельзя.")
    blockers += [
        v
        for v in dict.fromkeys(report.limitations)
        if not v.startswith(
            (
                "CPA и CR",
                "ДРР не рассчитан",
                "CR относится",
                "Недостаточный объём",
                "Предыдущий период",
                "Конверсии могут",
            )
        )
    ]
    if available:
        blockers += [
            v for v in dict.fromkeys(report.limitations) if v.startswith("Конверсии могут")
        ]
    if blockers:
        lines += ["", "Ограничения анализа:", *[f"• {v}" for v in dict.fromkeys(blockers)]]
    lines += ["", "Следующий шаг:"]
    if direct == "no_data":
        lines.append(
            "Откройте статистику этого клиента в Директе за указанные даты. Если реклама не работала — выберите период с показами. Если статистика есть — нужно проверить её загрузку в боте."
        )
    elif report.current.spend is None:
        lines.append(
            "Сначала восстановить получение статистики Директа; выводы об эффективности пока преждевременны."
        )
    elif not report.main_goal_ids:
        lines.append(
            "Определить, какие цели означают заявку или покупку, и настроить их для клиента. Остальные события использовать для анализа поведения."
        )
    else:
        lines += list(dict.fromkeys(s.next_check for s in useful[:3])) or [
            "Продолжить наблюдение за показателями."
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
        if report.source_status.get("Директ") == "no_data":
            lines.append("Директ не вернул статистику за период. Эффективность оценить нельзя.")
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
            if getattr(report.current, key) is not None:
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
