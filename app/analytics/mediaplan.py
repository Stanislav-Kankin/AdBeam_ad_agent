"""Plan versus fact for the current month of a client's media plan: how much of the
budget and the leads is done, where the month will end at the current pace, and,
when the plan has a reward scale, which KPI step that forecast lands on."""

import calendar
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.analytics.diagnostics import snapshot_metrics
from app.analytics.periods import DateRange, today_moscow
from app.domain.mediaplan import MediaPlan
from app.domain.reports import DataStatus
from app.integrations.mediaplan import MediaPlanError, source_hash
from app.reporting.formatter import number_text, plural

logger = logging.getLogger(__name__)

# Russian VAT since 2026; Direct reports spend without it.
VAT = Decimal("1.22")
MONTHS = (
    "январь",
    "февраль",
    "март",
    "апрель",
    "май",
    "июнь",
    "июль",
    "август",
    "сентябрь",
    "октябрь",
    "ноябрь",
    "декабрь",
)


def month_name(key):
    year, month = key.split("-")
    return f"{MONTHS[int(month) - 1]} {year}"


def money(value):
    value = Decimal(value)
    if abs(value) >= 1_000_000:
        return number_text(value / 1_000_000, 2) + " млн ₽"
    if abs(value) >= 10_000:
        return number_text(value / 1000, 1) + " тыс. ₽"
    return number_text(value, 0) + " ₽"


def count(value):
    return number_text(Decimal(value), 0)


def percent(part, whole):
    return Decimal(part) / Decimal(whole) * 100 if whole else None


@dataclass
class PlanFact:
    month: str
    elapsed: int
    days: int
    budget: Decimal | None
    leads_plan: Decimal | None
    spend: Decimal | None
    leads: Decimal | None
    notes: list[str] = field(default_factory=list)
    rewards: dict[str, Decimal] = field(default_factory=dict)
    scale_with_budget: bool = False

    @property
    def spend_forecast(self):
        return self.spend / self.elapsed * self.days if self.spend is not None else None

    @property
    def leads_forecast(self):
        return self.leads / self.elapsed * self.days if self.leads is not None else None

    @property
    def leads_target(self):
        """The plan grows with the budget when the sheet says so."""
        if self.leads_plan is None:
            return None
        if self.scale_with_budget and self.budget and self.spend_forecast:
            return self.leads_plan * max(Decimal(1), self.spend_forecast / self.budget)
        return self.leads_plan

    @property
    def completion(self):
        if self.leads_forecast is None or not self.leads_target:
            return None
        return percent(self.leads_forecast, self.leads_target)

    @property
    def reward_step(self):
        """Step "до N%" of the scale the forecast falls into, with its amount."""
        if not self.rewards or self.completion is None:
            return None
        steps = sorted(self.rewards, key=int)
        step = next((s for s in steps if self.completion <= int(s)), steps[-1])
        return step, self.rewards[step]

    @property
    def level(self):
        value = self.completion
        if value is None:
            return "⚪"
        return "🟢" if value >= 95 else "🟡" if value >= 80 else "🔴"


def plan_fact(plan, *, spend, leads, today=None, main_goal_ids=()):
    """Month to date through yesterday; on the 1st that is the whole previous month."""
    yesterday = (today or today_moscow()) - timedelta(days=1)
    key = f"{yesterday:%Y-%m}"
    month = plan.month(key)
    if month is None:
        return None
    budget = month.budget
    notes = []
    if budget is not None and plan.budget_vat == "with":
        budget = budget / VAT
        notes.append("Бюджет плана указан с НДС: для сравнения с Директом пересчитан без НДС 22%.")
    missing = [goal for goal in plan.goal_ids if goal not in set(main_goal_ids)]
    if plan.goal_ids and missing:
        notes.append(
            f"Не выбраны в боте основными {len(missing)} из {len(plan.goal_ids)} "
            f"{plural(len(plan.goal_ids), 'цели', 'целей', 'целей')} медиаплана: "
            "лиды могут не совпадать."
        )
    if plan.lead_definition:
        notes.append("Лид по медиаплану: " + plan.lead_definition.rstrip(".") + ".")
    return PlanFact(
        month=key,
        elapsed=yesterday.day,
        days=calendar.monthrange(yesterday.year, yesterday.month)[1],
        budget=budget,
        leads_plan=month.leads,
        spend=spend,
        leads=leads,
        notes=notes,
        rewards=month.rewards,
        scale_with_budget=plan.leads_scale_with_budget,
    )


def share(value, whole):
    value = percent(value, whole)
    return f"{number_text(value, 0)}%" if value is not None else "—"


def plan_fact_text(result):
    """Blocks with bold headings: a manager scans spend, leads, price and reward."""
    lines = [
        f"📅 **Медиаплан · {month_name(result.month)}**",
        f"Прошло {result.elapsed} из {result.days} дн. Прогноз — текущий темп до конца месяца.",
    ]
    expected = Decimal(result.elapsed) / result.days * 100
    if result.budget and result.spend is not None:
        lines += [
            "",
            "**💰 Расход**",
            f"{money(result.spend)} из {money(result.budget)} — **{share(result.spend, result.budget)}**"
            f" (по дням прошло {number_text(expected, 0)}%)",
            f"Прогноз: **{money(result.spend_forecast)} — "
            f"{share(result.spend_forecast, result.budget)}** плана",
        ]
    if result.leads_plan and result.leads is not None:
        lines += [
            "",
            f"**🎯 Лиды** {result.level}",
            f"{count(result.leads)} из {count(result.leads_target)} — "
            f"**{share(result.leads, result.leads_target)}**",
            f"Прогноз: **{count(result.leads_forecast)} — "
            f"{share(result.leads_forecast, result.leads_target)}** плана",
        ]
        if result.leads_target != result.leads_plan:
            lines.append(
                f"План увеличен с {count(result.leads_plan)}: прогноз расхода выше бюджета, "
                "а по условиям медиаплана лиды растут пропорционально."
            )
    if result.budget and result.leads_plan and result.leads:
        cpl, planned = result.spend / result.leads, result.budget / result.leads_plan
        sign = "+" if cpl >= planned else "−"
        lines += [
            "",
            "**💵 Цена лида**",
            f"**{money(cpl)}** · план {money(planned)} "
            f"({sign}{number_text(abs(percent(cpl - planned, planned)), 0)}%)",
        ]
    step = result.reward_step
    if step:
        top = result.rewards.get("100")
        lines += [
            "",
            "**🏆 Вознаграждение по шкале KPI**",
            f"По прогнозу ступень «до {step[0]}%»: **{money(step[1])}**"
            + (f" · при 100% — {money(top)}" if top and step[0] != "100" else ""),
        ]
    conditions = []
    if result.spend is None:
        conditions.append("Факт Директа за месяц сейчас недоступен: сравнение неполное.")
    elif result.leads is None and result.leads_plan:
        conditions.append("Лиды не посчитаны: выберите основные цели в «Данные и цели».")
    elif result.elapsed < result.days:
        conditions.append("Конверсии последних дней ещё дополняются.")
    conditions += result.notes
    if conditions:
        lines += ["", "**ℹ️ Условия**", *(f"• {item}" for item in conditions)]
    return "\n".join(lines)


def plan_fact_line(result):
    """One line for the weekly digest."""
    parts = []
    if result.budget and result.spend_forecast is not None:
        parts.append(f"расход {share(result.spend_forecast, result.budget)} плана")
    if result.completion is not None:
        parts.append(f"лиды {number_text(result.completion, 0)}% плана")
    if not parts:
        return None
    return f"медиаплан ({MONTHS[int(result.month[5:]) - 1]}, прогноз): " + ", ".join(parts)


def plan_preview(plan, limit=14):
    """What was read from the sheet, for the manager to confirm before saving."""
    lines = ["**План по месяцам**"]
    for month in plan.months[:limit]:
        parts = [f"**{month_name(month.month).capitalize()}**"]
        if month.budget is not None:
            parts.append(f"бюджет {money(month.budget)}")
        if month.leads is not None:
            parts.append(f"лиды {count(month.leads)}")
        if month.cpl is not None:
            parts.append(f"CPL {money(month.cpl)}")
        lines.append(" · ".join(parts))
    if len(plan.months) > limit:
        lines.append(f"…и ещё {len(plan.months) - limit} мес.")
    vat = {"without": "без НДС", "with": "с НДС", "unknown": "НДС не указан"}[plan.budget_vat]
    conditions = [f"Бюджет: {vat}."]
    if any(month.rewards for month in plan.months):
        conditions.append("Есть шкала вознаграждения по выполнению KPI.")
    if plan.leads_scale_with_budget:
        conditions.append("План по лидам растёт вместе с бюджетом.")
    if plan.lead_definition:
        conditions.append("Лид: " + plan.lead_definition)
    if plan.goal_ids:
        conditions.append("Цели Метрики из плана: " + ", ".join(plan.goal_ids[:12]))
    conditions += plan.notes[:3]
    lines += ["", "**Условия плана**", *(f"• {item}" for item in conditions)]
    return "\n".join(lines)


class MediaPlans:
    def __init__(self, reader, checks):
        self.reader, self.checks = reader, checks

    @property
    def repository(self):
        return self.checks.repository

    async def stored(self, client_id):
        record = await self.repository.media_plan(client_id)
        if not record or not record["plan"]:
            return None
        return MediaPlan.model_validate(record["plan"])

    async def progress(self, client, today=None):
        plan = await self.stored(client.id)
        if plan is None:
            return None
        yesterday = (today or today_moscow()) - timedelta(days=1)
        if plan.month(f"{yesterday:%Y-%m}") is None:
            return None
        snapshot = await self.checks.snapshot(
            client, DateRange(start=yesterday.replace(day=1), end=yesterday), quick=False
        )
        spend = leads = None
        if snapshot.direct.status == DataStatus.OK:
            metrics = snapshot_metrics(snapshot)
            spend, leads = metrics.spend, metrics.conversions
        return plan_fact(
            plan,
            spend=spend,
            leads=leads,
            today=today,
            main_goal_ids=client.direct.main_goal_ids,
        )

    async def digest_lines(self, clients):
        """Client id → media plan pace line for the weekly digest."""
        lines = {}
        for client in clients:
            try:
                result = await self.progress(client)
            except Exception as exc:
                logger.warning(
                    "Media plan pace failed client=%s (%s)", client.id, type(exc).__name__
                )
                continue
            line = plan_fact_line(result) if result else None
            if line:
                lines[client.id] = line
        return lines

    async def refresh(self, client_id, *, force=False):
        """Re-read a saved sheet; the model runs only when its text changed."""
        record = await self.repository.media_plan(client_id)
        if not record or not record["url"]:
            return False
        try:
            text = await self.reader.fetch(record["url"])
            digest = source_hash(text)
            if digest == record["source_hash"] and not force:
                await self.repository.update_media_plan(client_id, checked_at=now(), error=None)
                return False
            plan = await self.reader.extract(text)
        except MediaPlanError as exc:
            await self.repository.update_media_plan(client_id, checked_at=now(), error=str(exc))
            return False
        await self.repository.update_media_plan(
            client_id,
            plan=plan.model_dump(mode="json"),
            source_hash=digest,
            checked_at=now(),
            error=None,
        )
        logger.info("Media plan updated from its sheet client=%s", client_id)
        return True

    async def refresh_all(self):
        for record in await self.repository.media_plans():
            try:
                await self.refresh(record["client_id"])
            except Exception as exc:
                logger.warning(
                    "Media plan refresh failed client=%s (%s)",
                    record["client_id"],
                    type(exc).__name__,
                )


def now():
    return datetime.now(UTC)
