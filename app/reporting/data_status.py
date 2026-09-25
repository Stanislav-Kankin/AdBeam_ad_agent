from datetime import UTC, datetime, timedelta

from app.analytics.periods import MOSCOW, make_period


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def stamp(value):
    return utc(value).astimezone(MOSCOW).strftime("%d.%m.%Y %H:%M МСК")


async def describe_data(runtime, chat_id, client_id):
    client = runtime.registry.require(chat_id, client_id)
    period = make_period("30d").current
    repo = runtime.checks.repository
    data = await repo.data_status(client.id, period)
    now = datetime.now(UTC)
    lines = [f"Состояние данных · {client.name}", f"Дневное хранилище: {period.label()}"]
    for source, label in (("direct", "Директ"), ("metrica", "Метрика")):
        good = {
            r["day"]
            for r in data["daily"]
            if r["quality"] == "full"
            and r[source] in ("ok", "no_data")
            and utc(r["refresh_after"]) > now
        }
        stored = {r["day"] for r in data["daily"]}
        lines.append(
            f"{label}: свежие данные за {len(good)} из {period.days} дней; "
            f"не загружено {period.days - len(stored)} дней"
        )
        gaps = [
            period.start + timedelta(days=i)
            for i in range(period.days)
            if str(period.start + timedelta(days=i)) not in good
        ]
        if gaps:
            dates = ", ".join(day.strftime("%d.%m") for day in gaps[:5])
            lines.append(
                f"Нужна загрузка или обновление: {dates}"
                + (f" и ещё {len(gaps) - 5} дн." if len(gaps) > 5 else "")
            )
    lines.append(
        "Нулевой трафик считается загруженными данными. Неполные и устаревшие дни не считаются свежими."
    )
    latest = data["latest"]
    states = {
        "ok": "получены",
        "no_data": "нет активности",
        "insufficient": "неполные",
        "unavailable": "недоступны",
        "not_checked": "не проверялись",
    }
    if latest:
        lines += [
            "",
            f"Последний сохранённый срез: {latest['period_start']} — {latest['period_end']}",
            f"Получен: {stamp(latest['captured_at'])}",
            f"Директ: {states.get(latest['direct'], 'неизвестно')}; Метрика: {states.get(latest['metrica'], 'неизвестно')}",
            "Кеш среза: "
            + ("актуален" if utc(latest["expires_at"]) > now else "требует обновления"),
        ]
        limits = [*(latest["direct_limits"] or []), *(latest["metrica_limits"] or [])]
        for limit in list(dict.fromkeys(limits))[:3]:
            lines.append("• " + str(limit)[:220])
    else:
        lines.append("\nСохранённых срезов пока нет. Запустите проверку клиента.")
    lines.append("\nРазрезы Директа — полностью загруженные свежие дни:")
    for dim, label in (
        ("device", "Устройства"),
        ("geo", "География"),
        ("search", "Запросы"),
        ("placement", "Площадки"),
    ):
        items = [v for k, v in data["dimensions"].items() if k[2] == dim]
        complete = sum(
            v["last"] is not None and v["pages"] == set(range(v["last"] + 1)) for v in items
        )
        lines.append(
            f"{label}: {complete}/{period.days}; начато, но не завершено: {len(items) - complete}"
        )
    counters = await repo.client_counters(client.id)
    selected = [c for c in counters if c["selected"]]
    lines += [
        "",
        f"Основных целей: {len(client.direct.main_goal_ids)} из 10"
        + (
            " (взяты автоматически из настроек кампаний)"
            if client.direct.goals_source == "campaigns"
            else ""
        ),
        f"Выбрано счётчиков в каталоге: {len(selected)}",
    ]
    for c in selected[:5]:
        lines.append(f"• {c['id']}: {c['status']} · проверено {stamp(c['checked_at'])}")
    if not client.direct.main_goal_ids:
        lines.append(
            "Основные цели не выбраны; при следующей проверке бот возьмёт их из "
            "настроек кампаний. Выбрать вручную — «Данные и цели»."
        )
    job = runtime.schedule.scheduler.get_job("warehouse_warm") if runtime.schedule else None
    eligible = client.id in {
        c.id for c in runtime.registry.visible(runtime.settings.telegram_report_chat_id)
    }
    if job and eligible:
        lines.append(
            f"\nФоновая загрузка включена: последние {runtime.settings.warehouse_backfill_days} дней."
        )
        if getattr(job, "next_run_time", None):
            lines.append(f"Следующий общий проход: {stamp(job.next_run_time)}")
        lines.append("Место клиента в очереди и срок завершения не фиксируются.")
    else:
        lines.append("\nФоновая загрузка для этого клиента не запланирована.")
    lines.append("Доступы в API сейчас не проверялись: экран читает сохранённые данные.")
    return "\n".join(lines)
