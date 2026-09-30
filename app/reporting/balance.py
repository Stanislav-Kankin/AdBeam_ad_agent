"""/balance: model spend recorded by the bot itself.

The Anthropic API gives no account balance to an ordinary API key, so the bot sums
the tokens of every call it made at the prices set in .env and, if the top-up amount
and date are set, shows what is left of it.
"""

from datetime import UTC, datetime, time, timedelta

from app.analytics.periods import MOSCOW, today_moscow


def money(value):
    return f"${value:,.2f}".replace(",", " ").replace(".", ",")


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
