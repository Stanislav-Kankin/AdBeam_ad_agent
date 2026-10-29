"""/balance: model spend recorded by the bot itself.

The Anthropic API gives no account balance to an ordinary API key, so the bot sums
the tokens of every call it made at the prices set in .env and, if the top-up amount
and date are set, shows what is left of it.
"""

from datetime import UTC, datetime, time, timedelta

from app.analytics.periods import MOSCOW, today_moscow


def money(value):
    return f"${value:,.2f}".replace(",", " ").replace(".", ",")


async def people(runtime):
    """Telegram id → readable name for everyone the bot knows."""
    names = {}
    for uid, (username, full_name) in getattr(runtime.registry, "user_names", {}).items():
        names[uid] = full_name or (f"@{username}" if username else None)
    for uid, member in (await runtime.checks.repository.bot_users()).items():
        names[uid] = member.get("full_name") or (
            f"@{member['username']}" if member.get("username") else names.get(uid)
        )
    return {uid: f"{name} · {uid}" if name else str(uid) for uid, name in names.items()}


async def feedback_text(runtime, limit=15):
    """/feedback: the latest ratings of answers and reports, newest first."""
    items = await runtime.checks.repository.feedback(limit)
    if not items:
        return "Оценок пока нет. Кнопки 👍/👎 стоят под ответами и отчётами."
    names = await people(runtime)
    good = sum(item["rating"] > 0 for item in items)
    lines = [f"💬 Последние оценки: {len(items)} · 👍 {good} · 👎 {len(items) - good}", ""]
    for item in items:
        mark = "👍" if item["rating"] > 0 else "👎"
        who = names.get(item["user_id"], str(item["user_id"]))
        when = item["created_at"].astimezone(MOSCOW)
        lines.append(f"{mark} {when:%d.%m %H:%M} · {who}")
        lines.append("   " + item["excerpt"].replace("\n", " ")[:160])
        if item["comment"]:
            lines.append("   💬 " + item["comment"][:500])
    return "\n".join(lines)


async def balance_text(runtime):
    settings, repo = runtime.settings, runtime.checks.repository
    today = today_moscow()

    def since(day):
        return datetime.combine(day, time.min, MOSCOW).astimezone(UTC)

    rows = [
        ("Сегодня", await repo.llm_spend(since(today))),
        ("7 дней", await repo.llm_spend(since(today - timedelta(days=6)))),
        ("30 дней", await repo.llm_spend(since(today - timedelta(days=29)))),
        ("С начала месяца", await repo.llm_spend(since(today.replace(day=1)))),
    ]
    model = (
        settings.anthropic_model
        if settings.llm_provider == "anthropic" and settings.anthropic_api_key.get_secret_value()
        else settings.deepseek_model
    )
    lines = [f"💳 Расход на модель · {model}", ""]
    for label, spend in rows:
        lines.append(
            f"{label}: {money(spend['cost_usd'])} · обращений {spend['calls']} · "
            f"токенов {spend['input'] + spend['output']:,}".replace(",", " ")
        )
    month = await repo.llm_spend_by_user(since(today.replace(day=1)))
    if month:
        names = await people(runtime)
        lines += ["", "По людям с начала месяца:"]
        for uid, cost, calls in month[:10]:
            who = names.get(int(uid), uid) if uid and uid.isdigit() else "сводки и фоновые задачи"
            lines.append(f"{who}: {money(cost)} · обращений {calls}")
    if settings.llm_budget_usd is not None:
        start = settings.llm_budget_since or today.replace(day=1)
        spent = (await repo.llm_spend(since(start)))["cost_usd"]
        lines += [
            "",
            f"Пополнено {money(settings.llm_budget_usd)} с {start:%d.%m.%Y}, "
            f"потрачено {money(spent)}.",
            f"Остаток по расчёту бота: {money(max(0.0, settings.llm_budget_usd - spent))}",
        ]
    else:
        lines += [
            "",
            "Остаток не считается: задайте в .env LLM_BUDGET_USD (сумма пополнения) "
            "и LLM_BUDGET_SINCE (дата, ГГГГ-ММ-ДД).",
        ]
    lines += [
        "",
        "Это оценка по токенам и ценам из .env; точный остаток — в консоли Anthropic "
        "(platform.claude.com → Billing).",
    ]
    return "\n".join(lines)
