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

# How lead goals are attributed to Direct; Metrica's default report model first.
ATTRIBUTIONS = {
    "LYDC": "последний переход из Яндекс Директа, Метрика",
    "lastsign": "последний значимый переход, Метрика",
}
# Metrica's names of those models. Agencies read Direct leads by the last Direct
# click: Grand Line's specialist got the same numbers within a day's difference.
METRICA_MODELS = {"LYDC": "last_yandex_direct_click", "lastsign": "lastsign"}
DEFAULT_ATTRIBUTION = "LYDC"
# Visits whose last significant source is a Yandex Direct ad.
DIRECT_VISITS = "ym:s:<attribution>AdvEngine=='ya_direct'"
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


# Lead sources a plan may count that reach neither Direct nor Metrica goals.
UNSEEN_SOURCES = {
    "calltouch": "звонки Calltouch",
    "колтач": "звонки Calltouch",
    "crm": "данные CRM",
    "црм": "данные CRM",
    "roistat": "данные Roistat",
}


def unseen_sources(definition):
    text = (definition or "").casefold()
    return list(dict.fromkeys(name for word, name in UNSEEN_SOURCES.items() if word in text))


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
    # Sources the plan counts as leads that the bot cannot see (Calltouch, CRM).
    unseen: list[str] = field(default_factory=list)
    # Goal name → reaches, so a manager can check the leads against Metrica.
    breakdown: dict[str, Decimal] = field(default_factory=dict)
    # (monday or the 1st, sunday or the last complete day, spend, leads)
    weeks: list = field(default_factory=list)
    # The sheet's budget is with VAT: its amounts are shown with VAT too.
    vat_included: bool = False

    @property
    def first_day(self):
        return f"01.{self.month[5:]}"

    @property
    def last_day(self):
        return f"{self.elapsed:02d}.{self.month[5:]}"

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
        """(step "до N%", amount, below) for the forecast; below — under the lowest step
        the sheet gave, where the bot cannot tell the step. None when leads are not
        fully visible: a reward from undercounted leads would mislead."""
        if not self.rewards or self.completion is None or self.unseen:
            return None
        steps = sorted(self.rewards, key=int)
        gap = int(steps[1]) - int(steps[0]) if len(steps) > 1 else 10
        if self.completion <= int(steps[0]) - gap:
            return steps[0], self.rewards[steps[0]], True
        step = next((s for s in steps if self.completion <= int(s)), steps[-1])
        return step, self.rewards[step], False

    @property
    def level(self):
        value = self.completion
        if value is None or self.unseen:
            return "⚪"
        return "🟢" if value >= 95 else "🟡" if value >= 80 else "🔴"


def plan_fact(
    plan,
    *,
    spend,
    leads,
    today=None,
    main_goal_ids=(),
    lead_goals=None,
    attribution=DEFAULT_ATTRIBUTION,
    breakdown=None,
):
    """Month to date through yesterday; on the 1st that is the whole previous month.
    ``lead_goals``: Metrica goals agreed as leads for this client; leads then come from
    them and the plan's own lead definition no longer warns about unseen sources."""
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
    elif budget is not None and plan.budget_vat == "unknown":
        notes.append(
            "В медиаплане не указано, с НДС ли бюджет: сравниваю как будто без НДС. Если "
            "бюджет с НДС, переключите это в меню медиаплана."
        )
    if lead_goals:
        notes.append(
            f"Лиды — достижения {len(lead_goals)} "
            f"{plural(len(lead_goals), 'цели', 'целей', 'целей')} в визитах из Директа "
            f"({ATTRIBUTIONS.get(attribution, attribution)}), как настроено для клиента."
        )
    else:
        missing = [goal for goal in plan.goal_ids if goal not in set(main_goal_ids)]
        if plan.goal_ids and missing:
            notes.append(
                f"Не выбраны в боте основными {len(missing)} из {len(plan.goal_ids)} "
                f"{plural(len(plan.goal_ids), 'цели', 'целей', 'целей')} медиаплана: "
                "лиды могут не совпадать. Настройте «Цели для лидов» в медиаплане."
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
        unseen=[] if lead_goals else unseen_sources(plan.lead_definition),
        breakdown=breakdown or {},
        vat_included=plan.budget_vat == "with",
    )


def share(value, whole):
    value = percent(value, whole)
    return f"{number_text(value, 0)}%" if value is not None else "—"


def plan_fact_text(result):
    """Blocks with bold headings: a manager scans spend, leads, price and reward."""
    lines = [
        f"📅 **Медиаплан · {month_name(result.month)}**",
        f"Факт за {result.first_day}–{result.last_day} ({result.elapsed} из {result.days} дн.). "
        "Прогноз — текущий темп до конца месяца.",
    ]
    expected = Decimal(result.elapsed) / result.days * 100
    if result.budget and result.spend is not None:
        lines += [
            "",
            "**💰 Расход** · без НДС, как в отчётах Директа",
            f"{money(result.spend)} из {money(result.budget)}"
            + (f" ({money(result.budget * VAT)} с НДС)" if result.vat_included else "")
            + f" — **{share(result.spend, result.budget)}**"
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
        if result.breakdown:
            ranked = sorted(result.breakdown.items(), key=lambda item: -item[1])
            parts = [f"{name} {count(value)}" for name, value in ranked[:2]]
            rest = ranked[2:]
            if rest:
                parts.append(
                    f"ещё {len(rest)} {plural(len(rest), 'цель', 'цели', 'целей')} "
                    f"{count(sum(value for _, value in rest))}"
                )
            lines.append("По целям: " + " · ".join(parts))
        if result.unseen:
            lines.append(
                f"⚠️ В лиды медиаплана входят {' и '.join(result.unseen)} — бот их не видит "
                "и считает только конверсии целей Метрики из Директа. Факт лидов занижен, "
                "процент выполнения неточный."
            )
        if result.leads_target != result.leads_plan:
            lines.append(
                f"План увеличен с {count(result.leads_plan)}: прогноз расхода выше бюджета, "
                "а по условиям медиаплана лиды растут пропорционально."
            )
    # The price of undercounted leads is inflated just as much: no comparison then.
    if result.budget and result.leads_plan and result.leads and not result.unseen:
        cpl, planned = result.spend / result.leads, result.budget / result.leads_plan
        sign = "+" if cpl >= planned else "−"
        lines += [
            "",
            "**💵 Цена лида**",
            f"**{money(cpl)}** · план {money(planned)}"
            + (f" без НДС ({money(planned * VAT)} с НДС)" if result.vat_included else "")
            + " "
            f"({sign}{number_text(abs(percent(cpl - planned, planned)), 0)}%)",
        ]
    step = result.reward_step
    if step:
        top = result.rewards.get("100")
        reward = (
            f"Прогноз ниже нижней ступени шкалы, которая есть в плане (до {step[0]}% — "
            f"{money(step[1])})"
            if step[2]
            else f"По прогнозу ступень «до {step[0]}%»: **{money(step[1])}**"
        )
        lines += [
            "",
            "**🏆 Вознаграждение по шкале KPI**",
            reward + (f" · при 100% — {money(top)}" if top and step[0] != "100" else ""),
        ]
    if result.weeks:
        lines += ["", "**📆 По неделям (пн–вс)** · % — от плана на эти дни"]
        for start, end, spend, leads in result.weeks:
            days = (end - start).days + 1
            parts = [f"{start:%d.%m}–{end:%d.%m}"]
            if spend is not None:
                parts.append(
                    f"расход {money(spend)}"
                    + (
                        f" ({share(spend, result.budget * days / result.days)})"
                        if result.budget
                        else ""
                    )
                )
            if leads is not None and not result.unseen:
                parts.append(
                    f"лиды {count(leads)}"
                    + (
                        f" ({share(leads, result.leads_plan * days / result.days)})"
                        if result.leads_plan
                        else ""
                    )
                )
                if spend and leads:
                    parts.append(f"лид {money(spend / leads)}")
            if days < 7:
                parts.append(f"{days} дн.")
            lines.append(" · ".join(parts))
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
    if result.unseen:
        parts.append("лиды: часть не видна боту (" + ", ".join(result.unseen) + ")")
    elif result.completion is not None:
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
        month = DateRange(start=yesterday.replace(day=1), end=yesterday)
        record = await self.repository.media_plan(client.id) or {}
        if record.get("budget_vat"):
            plan = plan.model_copy(update={"budget_vat": record["budget_vat"]})
        lead_goals = record.get("lead_goal_ids")
        attribution = record.get("lead_attribution") or DEFAULT_ATTRIBUTION
        spend, leads, reaches = await self.fact(client, month, lead_goals, attribution)
        breakdown = None
        if reaches:
            names = await self.goal_names(client)
            breakdown = {}
            for goal, value in reaches.items():
                name = names.get(goal, f"цель {goal}")
                breakdown[name] = breakdown.get(name, Decimal(0)) + value
        result = plan_fact(
            plan,
            spend=spend,
            leads=leads,
            today=today,
            main_goal_ids=client.direct.main_goal_ids,
            lead_goals=lead_goals,
            attribution=attribution,
            breakdown=breakdown,
        )
        # Specialists read a month by Monday–Sunday weeks; the edge weeks are cut
        # to the month and to the last complete day.
        start = month.start
        while start <= month.end:
            end = min(start + timedelta(days=6 - start.weekday()), month.end)
            week_spend, week_leads, _ = await self.fact(
                client, DateRange(start=start, end=end), lead_goals, attribution
            )
            result.weeks.append((start, end, week_spend, week_leads))
            start = end + timedelta(days=1)
        return result

    async def fact(self, client, period, lead_goals, attribution):
        """(spend, leads, goal → reaches) of a period; leads come from the agreed
        goals when the client has them, else from Direct's main goal conversions."""
        snapshot = await self.checks.snapshot(client, period, quick=False)
        spend = leads = reaches = None
        if snapshot.direct.status == DataStatus.OK:
            metrics = snapshot_metrics(snapshot)
            spend, leads = metrics.spend, metrics.conversions
        if lead_goals:
            try:
                reaches = await self.metrica_leads(client, period, lead_goals, attribution)
            except Exception as exc:
                logger.warning(
                    "Media plan leads failed client=%s (%s)", client.id, type(exc).__name__
                )
            leads = sum(reaches.values(), Decimal(0)) if reaches is not None else None
        return spend, leads, reaches

    async def lead_source(self, client, period):
        """Agreed leads for both halves of a report period, or None when the client
        has no lead goals or Metrica did not answer: the card then keeps Direct's."""
        record = await self.repository.media_plan(client.id) or {}
        goals = record.get("lead_goal_ids")
        if not goals:
            return None
        attribution = record.get("lead_attribution") or DEFAULT_ATTRIBUTION
        try:
            current = await self.metrica_leads(client, period.current, goals, attribution)
            previous = await self.metrica_leads(client, period.previous, goals, attribution)
        except Exception as exc:
            logger.warning("Report leads failed client=%s (%s)", client.id, type(exc).__name__)
            return None
        if current is None:
            return None
        label = (
            f"по {len(goals)} {plural(len(goals), 'цели', 'целям', 'целям')} для лидов "
            f"в визитах из Директа ({ATTRIBUTIONS.get(attribution, attribution)})"
        )
        return (
            sum(current.values(), Decimal(0)),
            sum(previous.values(), Decimal(0)) if previous is not None else None,
            label,
        )

    async def goal_names(self, client):
        return {
            str(goal["id"]): str(goal.get("name") or goal["id"]).removeprefix("Конверсия | ")
            for counter in await self.repository.client_counters(client.id)
            for goal in counter["goals"] or []
        }

    async def metrica_leads(self, client, period, goal_ids, attribution=DEFAULT_ATTRIBUTION):
        """Goal → reaches in visits attributed to Direct by the chosen model; None
        without a Metrica counter."""
        counters = client.metrica.selected_counter_ids()
        if not counters:
            return None
        catalog = await self.repository.client_counters(client.id)
        owner = {
            str(goal["id"]): counter["id"]
            for counter in catalog
            if counter["id"] in counters
            for goal in counter["goals"] or []
        }
        by_counter = {}
        for goal in goal_ids:
            by_counter.setdefault(owner.get(goal, counters[0]), []).append(goal)
        reaches = {}
        for counter, goals in by_counter.items():
            # The Reporting API takes at most 20 metrics per query.
            for start in range(0, len(goals), 20):
                data = await self.checks.provider.metrica_query(
                    client,
                    counter,
                    period.start,
                    period.end,
                    metrics=[f"ym:s:goal{goal}reaches" for goal in goals[start : start + 20]],
                    dimensions=[],
                    filters=DIRECT_VISITS,
                    sort=None,
                    limit=1,
                    attribution=METRICA_MODELS[attribution],
                )
                for metric, value in data["totals"].items():
                    goal = metric.removeprefix("ym:s:goal").removesuffix("reaches")
                    reaches[goal] = Decimal(str(value or 0))
        return reaches

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
