from decimal import Decimal

from app.analytics.periods import DateRange
from app.analytics.rules import CONTEXT_SIGNALS, VOLUME_SIGNALS
from app.domain.reports import ClientReport, DirectData
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
AUDIENCE_NAMES = {
    "AGE_0_17": "до 18 лет",
    "AGE_18_24": "18–24 года",
    "AGE_25_34": "25–34 года",
    "AGE_35_44": "35–44 года",
    "AGE_45": "45 лет и старше",
    "AGE_45_54": "45–54 года",
    "AGE_55": "55 лет и старше",
    "GENDER_FEMALE": "женщины",
    "GENDER_MALE": "мужчины",
    "VERY_HIGH": "доход: топ 1%",
    "HIGH": "доход: 2–5%",
    "ABOVE_AVERAGE": "доход: 6–10%",
    "OTHER": "остальные уровни дохода",
    "UNKNOWN": "не определено",
}


def fmt(value):
    if value is None:
        return "не рассчитано"
    # Up to two decimals without trailing zeros: 3 084 288, not 3 084 288,00.
    return number_text(value, 2)


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


def _metric_change(report, key, *, compact=False):
    current = getattr(report.current, key)
    previous = getattr(report.previous, key)
    values = report.changes.get(key, {})
    percent = values.get("percent")
    absolute = values.get("absolute")
    tolerance = Decimal(str(report.targets.get("kpi_change_tolerance_percent", 3)))
    if percent is not None and abs(Decimal(percent)) <= tolerance:
        return f"{fmt_short(current, money=key in ('spend', 'cpc', 'cpa'))} · стабильно"
    if absolute is None:
        return f"сейчас {fmt_short(current)}; раньше {fmt_short(previous)}"
    absolute = Decimal(absolute)
    sign = "+" if absolute > 0 else ""
    unit = (
        " ₽" if key in ("spend", "cpc", "cpa") else " п.п." if key in ("ctr", "cr", "drr") else ""
    )
    delta = f"{sign}{fmt_short(absolute, money=key in ('spend', 'cpc', 'cpa'))}{unit}"
    if percent is not None:
        delta += f" ({'+' if Decimal(percent) > 0 else ''}{fmt_short(percent, money=True)}%)"
    if compact:
        return delta
    return (
        delta
        + f"\n  сейчас {fmt_short(current, money=key in ('spend', 'cpc', 'cpa'))}; "
        + f"раньше {fmt_short(previous, money=key in ('spend', 'cpc', 'cpa'))}"
    )


def main_metric(report: ClientReport) -> str:
    """The configured project KPI if it was calculated, else the best available metric."""
    configured = report.targets.get("kpi")
    if configured and getattr(report.current, configured) is not None:
        return configured
    if report.targets.get("target_drr") and report.current.drr is not None:
        return "drr"
    if report.current.cpa is not None:
        return "cpa"
    return "conversions" if report.current.conversions is not None else "spend"


STATUS_WORDS = {
    "green": "в норме",
    "yellow": "нужно внимание",
    "red": "критичный сигнал",
    "unknown": "оценка ограничена данными",
}
SHORT_NAMES = {
    "spend": "Расход",
    "impressions": "Показы",
    "clicks": "Клики",
    "ctr": "CTR",
    "cpc": "CPC",
    "conversions": "Конверсии",
    "cr": "CR",
    "cpa": "CPA",
    "revenue": "Выручка",
    "drr": "ДРР",
}
MONEY = ("spend", "cpc", "cpa", "revenue")
POINTS = ("ctr", "cr", "drr")
CARD_METRICS = ("spend", "conversions", "cpa", "clicks", "cpc", "ctr", "cr", "drr")
TABLE_SIGNALS = frozenset({"spend_change", "cpc_change", "cr_drop"})


def number_text(value, decimals):
    """Russian number with at most ``decimals`` digits and no trailing zeros (48, 24,06)."""
    text = f"{Decimal(value):,.{decimals}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text.replace(",", " ").replace(".", ",")


def value_text(key, value):
    """Readable value: whole rubles for large sums, kopecks only where they matter."""
    if value is None:
        return "—"
    number = Decimal(value)
    if key in MONEY:
        return number_text(number, 0 if abs(number) >= 100 else 2) + " ₽"
    if key in POINTS:
        return number_text(number, 2) + "%"
    return number_text(number, 2)


def signed(value, decimals=1):
    number = Decimal(value)
    return ("+" if number > 0 else "−" if number < 0 else "") + number_text(abs(number), decimals)


def tolerance_of(report):
    return Decimal(str(report.targets.get("kpi_change_tolerance_percent", 3)))


def delta_text(report, key):
    """Change first: that is what a specialist reads before the absolute value."""
    values = report.changes.get(key, {})
    percent, absolute = values.get("percent"), values.get("absolute")
    if percent is not None and abs(Decimal(percent)) <= tolerance_of(report):
        return "стабильно"
    if key in POINTS and absolute is not None:
        return f"{signed(absolute, 2)} п.п."
    if percent is not None:
        return f"{signed(percent)}%"
    return "нет базы сравнения"


def metric_line(report, key):
    current, previous = getattr(report.current, key), getattr(report.previous, key)
    return (
        f"{SHORT_NAMES[key]}: **{delta_text(report, key)}** · "
        f"{value_text(key, current)} / было {value_text(key, previous)}"
    )


def contextual_types(report):
    if report.targets.get("kpi_stable"):
        return CONTEXT_SIGNALS | VOLUME_SIGNALS
    return CONTEXT_SIGNALS


def expensive_campaigns(report, limit=3):
    """Campaigns whose CPA is far above the account average and that carry real spend."""
    account_cpa, spend = report.current.cpa, report.current.spend
    if account_cpa is None or not spend:
        return []
    target = report.targets.get("target_cpa")
    ceiling = max(Decimal(account_cpa) * Decimal("1.5"), Decimal(str(target or 0)))
    risks = []
    for row in report.drivers:
        current, previous = row.get("current", {}), row.get("previous", {})
        cpa = current.get("cpa")
        row_spend = Decimal(str(current.get("spend") or 0))
        if cpa is None or row_spend < Decimal(spend) * Decimal("0.03"):
            continue
        if Decimal(str(cpa)) < ceiling:
            continue
        ratio = Decimal(str(cpa)) / Decimal(account_cpa)
        risks.append(
            f"«{row['name']}»: CPA {value_text('cpa', cpa)} — в {number_text(ratio, 1)} "
            f"раза выше среднего по аккаунту; расход {value_text('spend', previous.get('spend'))}"
            f" → {value_text('spend', row_spend)}."
        )
    return risks[:limit]


def conclusion(report):
    """Deterministic one-to-two sentence conclusion; the model may replace it."""
    if report.source_status.get("Директ") == "no_data":
        return "Директ не вернул статистику за период: реклама не показывалась или данные ещё не готовы."
    if report.current.spend is None:
        return "Расход не загрузился из Директа — это проблема получения данных, а не нулевая активность."
    kpi = main_metric(report)
    name, delta = SHORT_NAMES[kpi], delta_text(report, kpi)
    target = report.targets.get({"cpa": "target_cpa", "drr": "target_drr"}.get(kpi, ""))
    current = getattr(report.current, kpi)
    if report.level == "green" or report.targets.get("kpi_stable"):
        text = f"{name} в пределах нормы."
    elif delta in ("стабильно", "нет базы сравнения"):
        text = f"{name} без существенных изменений, но есть сигналы ниже."
    else:
        text = f"{name} {delta} к прошлому периоду."
    if target is not None and current is not None and Decimal(current) > Decimal(str(target)):
        text = text[:-1] + f", выше цели {value_text(kpi, target)}."
    changes = [
        f"{SHORT_NAMES[key].lower()} {delta_text(report, key)}"
        for key in ("spend", "conversions")
        if key != kpi and delta_text(report, key) not in ("стабильно", "нет базы сравнения")
    ]
    if changes:
        text += f" Объём: {', '.join(changes)}."
    top = next((row for row in report.drivers if row.get("spend_delta")), None)
    if top:
        text += (
            f" Больше всего расход изменился в «{top['name']}» ({signed(top['spend_delta'], 0)} ₽)."
        )
    return text


def card(report: ClientReport, summary: str | None = None) -> str:
    """Main single-client answer: status, KPI, key deltas, concrete risks, data line."""
    kpi = main_metric(report)
    lines = ["🧪 MOCK — тестовые данные"] if report.mock else []
    lines.append(
        f"{ICONS.get(report.level, '⚪')} **{report.client_name}** · "
        f"{STATUS_WORDS.get(report.level, 'статус не определён')}"
    )
    lines.append(f"{report.period.current.label()} против {report.period.previous.label()}")
    target_key = {"cpa": "target_cpa", "drr": "target_drr"}.get(kpi)
    target = report.targets.get(target_key) if target_key else None
    kpi_line = (
        f"**{SHORT_NAMES[kpi]} {value_text(kpi, getattr(report.current, kpi))}** · "
        f"{delta_text(report, kpi)}"
    )
    if target is not None:
        kpi_line += f" · цель {value_text(kpi, target)}"
    lines += [kpi_line, "", (summary or conclusion(report)).replace("**", "").strip()]

    metrics = [
        key
        for key in CARD_METRICS
        if key != kpi
        and (getattr(report.current, key) is not None or getattr(report.previous, key) is not None)
    ]
    if metrics:
        lines += ["", "**Показатели** · изменение · сейчас / было"]
        lines += [metric_line(report, key) for key in metrics]

    # Account-level volume shifts are already bold in the metrics table above.
    contextual = contextual_types(report) | TABLE_SIGNALS
    alerts = [
        s for s in report.signals if s.type not in contextual and s.type != "tracking" and s.message
    ]
    # Concrete places (a named campaign) come right after critical alerts, before
    # account-level yellow signals, so the reader sees where the problem is.
    risks = [s.message for s in alerts if s.level == "red"]
    risks += expensive_campaigns(report)
    risks += [s.message for s in alerts if s.level != "red"]
    tracking = next((s for s in report.signals if s.type == "tracking"), None)
    if tracking:
        risks += tracking.actual.get("reasons", [])[:1]
    risks = list(dict.fromkeys(risks))[:5]
    lines += ["", "**⚠️ Требует внимания**" if risks else "**Рисков не найдено**"]
    lines += [f"{i}. {text}" for i, text in enumerate(risks, 1)]
    context = [
        s.message for s in report.signals if s.type in contextual - TABLE_SIGNALS and s.message
    ]
    if context:
        lines.append("Контекст: " + " ".join(dict.fromkeys(context[:3])))

    sources = ", ".join(
        f"{source} — {STATUS_NAMES.get(status, status)}"
        for source, status in report.source_status.items()
        if source in ("Директ", "Метрика")
    )
    footer = f"Данные: {sources}."
    if any(v.startswith("Конверсии могут") for v in report.limitations):
        footer += " Конверсии за последние дни ещё дополняются."
    lines += ["", footer]
    return redact("\n".join(lines))


def campaigns_view(report: ClientReport, limit=10) -> str:
    """Specialist drill-down: campaign contribution with CPA now and before."""
    lines = [
        f"📈 **Кампании · {report.client_name}**",
        f"{report.period.current.label()} против {report.period.previous.label()}",
        "Сортировка по вкладу в изменение расхода.",
        "",
    ]
    if not report.drivers:
        lines.append("Данных по кампаниям за оба периода нет.")
        return redact("\n".join(lines))
    for row in report.drivers[:limit]:
        current, previous = row.get("current", {}), row.get("previous", {})
        delta = Decimal(str(row.get("spend_delta") or 0))
        parts = [
            f"расход {value_text('spend', current.get('spend'))} "
            f"({signed(delta, 0)} ₽, было {value_text('spend', previous.get('spend'))})"
        ]
        if current.get("conversions") is not None or previous.get("conversions") is not None:
            parts.append(
                f"конверсии {fmt_short(current.get('conversions'))} "
                f"(было {fmt_short(previous.get('conversions'))})"
            )
        if current.get("cpa") is not None or previous.get("cpa") is not None:
            parts.append(
                f"CPA {value_text('cpa', current.get('cpa'))} "
                f"(было {value_text('cpa', previous.get('cpa'))})"
            )
        lines.append(f"• **{row['name']}**: " + "; ".join(parts))
    if len(report.drivers) > limit:
        lines.append(f"Показаны {limit} из {len(report.drivers)} кампаний.")
    return redact("\n".join(lines))


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
        "cpa_above_target",
        "cpa_change",
        "cr_drop",
        "cpc_change",
        "budget_pacing",
        "spend_change",
        "drr_high",
        "campaign_without_conversions",
        "device_cr_drop",
    }
    attention = [report for report in active if report.level in ("red", "yellow")]
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


def audience_report(client_name, payload) -> str:
    period = payload["period"]
    lines = [
        f"👥 Аудитория · {client_name}",
        "Период: " + DateRange(start=period["start"], end=period["end"]).label(),
        "",
        "Рекламный трафик Директа:",
    ]
    labels = {"age": "Возраст", "gender": "Пол", "income": "Доход"}
    for key in ("age", "gender", "income"):
        report = DirectData.model_validate(payload["direct"][key])
        rows = [row for row in report.rows if (row.totals.clicks or 0) > 0]
        total = sum(row.totals.clicks or 0 for row in rows)
        lines.append(f"{labels[key]}:")
        if not rows or not total:
            lines.append("• нет данных")
            continue
        for row in sorted(rows, key=lambda item: item.totals.clicks or 0, reverse=True)[:5]:
            share = Decimal(row.totals.clicks or 0) / Decimal(total) * 100
            cpa = (
                row.totals.spend / row.totals.conversions
                if row.totals.spend is not None and (row.totals.conversions or 0) > 0
                else None
            )
            suffix = f"; CPA {value_text('cpa', cpa)}" if cpa is not None else ""
            lines.append(
                f"• {AUDIENCE_NAMES.get(row.name, row.name)}: "
                f"{number_text(share, 1)}% кликов{suffix}"
            )

    interests = payload.get("interests", {})
    lines += ["", "Долгосрочные интересы аудитории сайта (Метрика):"]
    if interests.get("rows"):
        for row in interests["rows"][:7]:
            lines.append(
                f"• {row['name']}: аффинити {fmt_short(row.get('affinity'), money=True)}; "
                f"пользователи {fmt_short(row.get('users'))}"
            )
    else:
        lines.append("• данные не получены")
    lines += [
        "",
        "Важно: возраст, пол и доход относятся к рекламе клиента в Директе; "
        "интересы — ко всему трафику выбранных счётчиков Метрики.",
    ]
    limitations = interests.get("limitations") or []
    if limitations:
        lines.append(f"Ограничения: {len(limitations)}. Подробности сохранены в диагностике.")
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
    ordered = sorted(
        reports,
        key=lambda report: (
            {"red": 0, "yellow": 1, "unknown": 2, "green": 3}.get(report.level, 2),
            -abs(Decimal(report.changes.get("spend", {}).get("percent") or 0)),
            -(report.current.spend or 0),
        ),
    )
    data_issues = [
        report
        for report in ordered
        if report.current.spend is None
        or any(
            report.source_status.get(source) not in (None, "ok", "not_checked", "no_data")
            for source in ("Директ", "Метрика")
        )
    ]
    issue_ids = {report.client_id for report in data_issues}
    attention = [
        report
        for report in ordered
        if report.level in ("red", "yellow") and report.client_id not in issue_ids
    ]
    stable = [
        report
        for report in ordered
        if report.client_id not in issue_ids and report not in attention
    ]
    lines = [
        "🧪 MOCK — тестовые данные"
        if any(r.mock for r in reports)
        else "📊 AdBeam Performance Analyst",
        f"{period.current.label()} против {period.previous.label()}",
        (
            f"Проверено {len(reports)} проектов: требуют внимания {len(attention)}, "
            f"без существенных сигналов {len(stable)}, проблемы данных {len(data_issues)}."
        ),
        "",
        "Главные проекты:",
    ]
    candidates = [*attention, *data_issues]
    if not candidates:
        candidates = [report for report in ordered if report.current.spend is not None]
    for report in candidates[:5]:
        if report.source_status.get("Директ") == "no_data":
            fact = "нет статистики Директа за период"
        elif report.current.spend is None:
            fact = "расход не загрузился"
        else:
            spend = f"расход {fmt_short(report.current.spend, money=True)} ₽"
            delta = report.changes.get("spend", {}).get("percent")
            fact = spend + (
                f" ({'+' if Decimal(delta) > 0 else ''}{fmt_short(delta, money=True)}%)"
                if delta is not None
                else ""
            )
        signal = next((s.message for s in report.signals if s.type != "tracking"), "")
        lines.append(
            f"{ICONS.get(report.level, '⚪')} {report.client_name}: {fact}"
            + (f". {signal}" if signal else ".")
        )
    if len(candidates) > 5:
        lines.append(f"Ещё проектов в этой группе: {len(candidates) - 5}.")

    actions = []
    for report in attention:
        action = next((s.next_check for s in report.signals if s.next_check), None)
        if action:
            actions.append(f"{report.client_name}: {action}")
    if data_issues:
        actions.append(f"Восстановить или проверить источники у {len(data_issues)} проектов.")
    actions = list(dict.fromkeys(actions))
    if actions:
        lines += ["", "Что сделать:", *[f"{i}. {v}" for i, v in enumerate(actions[:3], 1)]]
    if errors:
        lines += ["", "Не завершены проверки:"]
        lines.extend(f"• {error}" for error in list(dict.fromkeys(errors))[:3])
        if len(set(errors)) > 3:
            lines.append(f"• Ещё: {len(set(errors)) - 3}.")
    lines += ["", "Для технической расшифровки запустите проверку нужного клиента."]
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
