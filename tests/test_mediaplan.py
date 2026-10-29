import json
from datetime import date, timedelta
from decimal import Decimal

import httpx
import pytest
from aiogram import Bot
from aiogram.methods import SendMessage
from aiogram.types import CallbackQuery, Update, User

from app.analytics.mediaplan import plan_fact, plan_fact_line, plan_fact_text, plan_preview
from app.bot.handlers import build_dispatcher
from app.domain.mediaplan import MediaPlan, sheet_location
from app.integrations.deepseek import LLMMessage
from app.integrations.mediaplan import MediaPlanError, MediaPlanReader, parse_reply
from tests.test_bot_schedule import FakeTelegram, message_update

SHEET = (
    "https://docs.google.com/spreadsheets/d/1GGcRtNwxMo8PA5ab_RhHL30VNvrcN37woGWF5fPTGyA"
    "/edit?gid=124987712#gid=124987712"
)
# Grand Line's sheet as the model reads it: reward steps and a lead plan per month.
GRAND = {
    "months": [
        {
            "month": "2026-10",
            "budget": "4 676 246",
            "leads": "5257",
            "rewards": {"50": "45406", "90": "136218", "100": "181625", "120": "272437"},
        },
        {"month": "2026-11", "budget": 3209367, "leads": 3390, "rewards": {}},
    ],
    "budget_vat": "unknown",
    "lead_definition": "целевой звонок Calltouch + заказ через корзину + избранная цель",
    "goal_ids": ["37618142", "29872089"],
    "leads_scale_with_budget": True,
    "notes": [],
}


def test_sheet_link_gives_spreadsheet_and_sheet():
    assert sheet_location(SHEET) == ("1GGcRtNwxMo8PA5ab_RhHL30VNvrcN37woGWF5fPTGyA", "124987712")
    assert sheet_location(SHEET.split("/edit")[0])[1] is None
    for bad in ("https://evil.example/spreadsheets/d/abc", "docs.google.com/spreadsheets/d/x"):
        with pytest.raises(ValueError):
            sheet_location(bad)


def test_model_reply_becomes_a_validated_plan():
    plan = parse_reply("```json\n" + json.dumps(GRAND, ensure_ascii=False) + "\n```")
    october = plan.month("2026-10")
    assert october.budget == Decimal("4676246") and october.leads == 5257
    assert october.rewards["90"] == Decimal("136218")
    assert plan.goal_ids == ["37618142", "29872089"]
    assert "Октябрь 2026 · бюджет 4,68 млн ₽ · лиды 5 257" in plan_preview(plan)
    with pytest.raises(MediaPlanError, match="не нашлось плана"):
        parse_reply('{"error": "нет бюджета по месяцам"}')
    with pytest.raises(MediaPlanError):
        parse_reply('{"months": [{"month": "октябрь", "budget": 1}]}')


async def test_sheet_is_fetched_through_google_redirect_only():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        if request.url.host == "docs.google.com":
            return httpx.Response(
                307, headers={"location": "https://doc-0g.googleusercontent.com/export/x"}
            )
        return httpx.Response(200, text="Месяц,Бюджет\n", headers={"content-type": "text/csv"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await MediaPlanReader(http, None).fetch(SHEET) == "Месяц,Бюджет\n"
    assert calls[0].endswith("/export?format=csv&gid=124987712")

    def closed(request):
        if request.url.host == "docs.google.com":
            return httpx.Response(307, headers={"location": "https://evil.example/steal"})
        return httpx.Response(200, text="ok", headers={"content-type": "text/csv"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(closed)) as http:
        with pytest.raises(MediaPlanError, match="Нет доступа"):
            await MediaPlanReader(http, None).fetch(SHEET)

    def login(request):
        return httpx.Response(200, text="<html>", headers={"content-type": "text/html"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(login)) as http:
        with pytest.raises(MediaPlanError, match="Все, у кого есть ссылка"):
            await MediaPlanReader(http, None).fetch(SHEET)


async def test_model_gets_rules_as_system_and_failures_are_explained():
    seen = []

    class Recorder:
        async def complete(self, messages, tools):
            seen.append(messages)
            return LLMMessage(content=json.dumps(GRAND, ensure_ascii=False))

    plan = await MediaPlanReader(None, Recorder()).extract("Месяц,Бюджет\n,,\nОктябрь,1\n")
    assert plan.month("2026-10")
    system, user = seen[0]
    assert system["role"] == "system" and "Верни только JSON" in system["content"]
    assert user["content"] == "CSV:\nМесяц,Бюджет\nОктябрь,1"

    class Broken:
        async def complete(self, messages, tools):
            raise RuntimeError("400")

    with pytest.raises(MediaPlanError, match="Модель не ответила"):
        await MediaPlanReader(None, Broken()).extract("Месяц\n")


def test_plan_fact_forecasts_month_and_reward_step():
    plan = MediaPlan.model_validate(parse_reply(json.dumps(GRAND)).model_dump(mode="json"))
    # 28 of 31 October days are complete on the 29th.
    result = plan_fact(
        plan,
        spend=Decimal("3920000"),
        leads=Decimal("4100"),
        today=date(2026, 10, 29),
        main_goal_ids=["37618142"],
    )
    assert (result.elapsed, result.days) == (28, 31)
    assert round(result.leads_forecast) == 4539
    assert result.leads_target == Decimal("5257")
    assert result.reward_step == ("90", Decimal("136218"))
    text = plan_fact_text(result)
    assert "Медиаплан · октябрь 2026" in text and "прошло 28 из 31" in text
    assert "ступень «до 90%»" in text and "при 100% — 181,6 тыс. ₽" in text
    assert "1 из них не выбраны в боте" in text
    assert (
        plan_fact_line(result) == "медиаплан (октябрь, прогноз): расход 93% плана, лиды 86% плана"
    )
    # Over budget: the lead plan grows with the spend, as the sheet says.
    over = plan_fact(
        plan, spend=Decimal("5000000"), leads=Decimal("4100"), today=date(2026, 10, 29)
    )
    assert over.leads_target > Decimal("5257")
    assert "План по лидам увеличен" in plan_fact_text(over)
    # On the 1st the whole previous month is judged.
    assert plan_fact(plan, spend=1, leads=1, today=date(2026, 11, 1)).elapsed == 31
    assert plan_fact(plan, spend=1, leads=1, today=date(2027, 3, 5)) is None


def test_plan_with_vat_is_compared_without_it():
    payload = {**GRAND, "budget_vat": "with", "leads_scale_with_budget": False}
    plan = parse_reply(json.dumps(payload))
    result = plan_fact(plan, spend=Decimal(1), leads=Decimal(1), today=date(2026, 10, 29))
    assert round(result.budget) == round(Decimal("4676246") / Decimal("1.22"))
    assert "пересчитан без НДС" in plan_fact_text(result)


class FakeLLM:
    async def complete(self, messages, tools):
        return LLMMessage(content=json.dumps(GRAND, ensure_ascii=False))


async def test_report_asks_for_media_plan_then_saves_and_compares(runtime, monkeypatch):
    from app.analytics.periods import today_moscow

    # Plan versus fact is judged on the month of the last complete day.
    month = f"{today_moscow() - timedelta(days=1):%Y-%m}"
    plan = {**GRAND, "months": [{**GRAND["months"][0], "month": month}]}

    class Reader(MediaPlanReader):
        async def fetch(self, url):
            return "Месяц,Бюджет,Лиды\n"

    reader = Reader(None, FakeLLM())

    async def extract(text, today=None):
        return parse_reply(json.dumps(plan, ensure_ascii=False))

    reader.extract = extract
    runtime.mediaplans.reader = reader
    session = FakeTelegram()
    async with Bot(token="555:THIS_IS_A_SYNTHETIC_TEST_TOKEN", session=session) as bot:
        dp = build_dispatcher(runtime)

        async def click(label):
            keyboard = next(
                m.reply_markup
                for m in reversed(session.sent)
                if getattr(m, "reply_markup", None) is not None
            ).inline_keyboard
            data = next(b.callback_data for row in keyboard for b in row if label in b.text)
            await dp.feed_update(
                bot,
                Update(
                    update_id=2,
                    callback_query=CallbackQuery(
                        id="nav",
                        from_user=User(id=1, is_bot=False, first_name="Admin"),
                        chat_instance="test",
                        message=message_update("menu").message,
                        data=data,
                    ),
                ),
            )

        def texts():
            return [m.text for m in session.sent if isinstance(m, SendMessage)]

        await dp.feed_update(bot, message_update("/menu"))
        await click("Клиенты")
        await click("West")
        await click("Подробная проверка")
        await click("7 дней")
        assert "нет медиаплана" in session.sent[-1].text
        await click("Добавить ссылку на медиаплан")
        await dp.feed_update(bot, message_update("не ссылка"))
        assert "Нужна ссылка" in texts()[-1]
        await dp.feed_update(bot, message_update(SHEET))
        await runtime.jobs.close()
        assert "Проверьте цифры" in session.sent[-1].text
        await click("Всё верно, сохранить")
        await runtime.jobs.close()
        stored = await runtime.checks.repository.media_plan("west_export")
        assert stored["url"] == SHEET and stored["plan"]["months"][0]["month"] == month
        # The paused report ran with the plan versus fact under it.
        assert any("📅 Медиаплан ·" in (text or "") for text in texts())

        # Next time the report runs straight away; the plan screen offers plan versus fact.
        await dp.feed_update(bot, message_update("/menu"))
        await click("Клиенты")
        await click("West")
        await click("Медиаплан")
        assert "Месяцев в плане: 1" in session.sent[-1].text
        await click("Убрать медиаплан")
        assert (await runtime.checks.repository.media_plan("west_export"))["plan"] is None
        await click("К отчёту")
        await click("Подробная проверка")
        await click("7 дней")
        await click("Не спрашивать по этому клиенту")
        await runtime.jobs.close()
        assert (await runtime.checks.repository.media_plan("west_export"))["dismissed"]
