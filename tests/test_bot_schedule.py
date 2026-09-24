import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageReplyMarkup,
    EditMessageText,
    GetMe,
    SendMessage,
    SendPhoto,
)
from aiogram.types import Chat, Message, Update, User

from app.bot.commands import parse_command
from app.bot.handlers import build_dispatcher
from app.bot.jobs import BackgroundJobs
from app.reporting.formatter import split_message
from app.scheduler.jobs import DailySchedule


class FakeTelegram(BaseSession):
    def __init__(self):
        super().__init__()
        self.sent = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):  # noqa: ASYNC109
        self.sent.append(method)
        if isinstance(method, GetMe):
            return User(id=555, is_bot=True, first_name="Test", username="adbeam_test_bot")
        if isinstance(method, AnswerCallbackQuery):
            return True
        if isinstance(method, DeleteMessage):
            return True
        if isinstance(method, SendMessage | SendPhoto | EditMessageReplyMarkup | EditMessageText):
            return Message(
                message_id=len(self.sent),
                date=datetime.now(UTC),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=getattr(method, "text", None),
            )
        raise AssertionError(type(method))

    async def stream_content(self, url, **kwargs):
        yield b""


def message_update(text, chat=123456789, user=1):
    return Update(
        update_id=1,
        message=Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=Chat(id=chat, type="private"),
            from_user=User(id=user, is_bot=False, first_name="User"),
            text=text,
        ),
    )


def test_commands_quotes_aliases_periods():
    parsed = parse_command('/check@adbeam_bot "West Экспорт" 14d')
    assert (parsed.name, parsed.client_query, parsed.period) == ("check", "West Экспорт", "14d")
    assert parse_command("/check West Экспорт yesterday").client_query == "West Экспорт"
    assert parse_command("/summary_all").period == "7d"
    with pytest.raises(ValueError):
        parse_command("/check_all 91d")


def test_telegram_splitting_unicode_preserves_content():
    original = ("🧪 Текст 🚀\n" * 3000) + "x" * 9000
    parts = split_message(original)
    assert "".join(parts) == original
    assert all(len(p.encode("utf-16-le")) // 2 <= 4000 for p in parts)
    assert len(parts) > 3


def test_report_entities_preserve_unicode_and_literal_markup():
    from app.bot.report_message import report_entities

    text = "📊 AdBeam\n<клиент & название>\nКлики: 100,00\nCPA, ₽: 30,00"
    raw = text.encode("utf-16-le")
    spans = [
        raw[e.offset * 2 : (e.offset + e.length) * 2].decode("utf-16-le")
        for e in report_entities(text)
    ]
    assert spans == ["📊 AdBeam", "Клики: 100,00", "CPA, ₽: 30,00"]


async def test_progress_is_replaced_and_cannot_overwrite_report(monkeypatch):
    from app.bot import report_message

    monkeypatch.setattr(report_message, "PROGRESS_INTERVAL", 0.01)
    session = FakeTelegram()
    async with Bot(token="555:THIS_IS_A_SYNTHETIC_TEST_TOKEN", session=session) as bot:
        presentation = report_message.ReportMessage(message_update("/check").message.as_(bot))
        await presentation.start({"stage": "Загрузка Метрики"})
        await asyncio.sleep(0.04)
        assert any(
            isinstance(m, EditMessageText) and "Загрузка Метрики" in m.text for m in session.sent
        )
        text = "Клики: 100,00\n" + "Данные 🚀\n" * 600
        await presentation.finish(text)
        count = len(session.sent)
        await asyncio.sleep(0.03)
        assert len(session.sent) == count
        edits = [m for m in session.sent if isinstance(m, EditMessageText)]
        assert edits[-1].text.startswith("Клики: 100,00")
        assert edits[-1].entities[0].type == "bold"
        assert edits[-1].parse_mode is None
        # First report chunk replaces the status; remaining chunks are sent afterward.
        sends = [m for m in session.sent if isinstance(m, SendMessage)]
        assert edits[-1].text + "".join(m.text for m in sends[1:]) == text


@pytest.mark.parametrize(
    "text",
    [
        "/start",
        "/help",
        "/clients",
        "/check_all 7d",
        '/check "West Экспорт" 14d',
        "/summary_all",
        "/summary grand_line 30d",
        '/chart "West Экспорт" 14d',
        "/cancel",
        "Почему у West Экспорт вырос CPA?",
    ],
)
async def test_telegram_commands_and_free_text_end_to_end(runtime, text):
    session = FakeTelegram()
    async with Bot(token="555:THIS_IS_A_SYNTHETIC_TEST_TOKEN", session=session) as bot:
        dp = build_dispatcher(runtime)
        await dp.feed_update(bot, message_update(text))
        await runtime.jobs.close()
    assert session.sent
    content = "\n".join(
        m.text for m in session.sent if isinstance(m, (SendMessage, EditMessageText))
    )
    assert "не завершилась" not in content and "Не удалось" not in content
    if text.startswith(("/check ", "/check_all")):
        assert "Проверка началась" in content and "MOCK" in content
    if text.startswith(("/chart", '/check "', "/summary grand")) or text.startswith("Почему"):
        assert any(isinstance(method, SendPhoto) for method in session.sent)
    if text.startswith(("/check_all", "/summary_all")):
        assert not any(isinstance(method, SendPhoto) for method in session.sent)


async def test_single_client_check_offers_on_demand_technical_details(runtime):
    from aiogram.types import CallbackQuery

    session = FakeTelegram()
    async with Bot(token="555:THIS_IS_A_SYNTHETIC_TEST_TOKEN", session=session) as bot:
        dp = build_dispatcher(runtime)
        await dp.feed_update(bot, message_update('/check "West Экспорт" 7d'))
        await runtime.jobs.close()
        offer = next(
            item
            for item in session.sent
            if isinstance(item, SendMessage) and item.text == "Дополнительные данные"
        )
        assert offer.reply_markup.inline_keyboard[0][0].text == "📈 Кампании"
        assert offer.reply_markup.inline_keyboard[1][0].text == "👥 Аудитория"
        assert offer.reply_markup.inline_keyboard[2][0].text == "⚙️ Технические данные"
        data = offer.reply_markup.inline_keyboard[2][0].callback_data
        callback = CallbackQuery(
            id="details",
            from_user=User(id=1, is_bot=False, first_name="User"),
            chat_instance="test",
            message=message_update("details").message,
            data=data,
        )
        before = len(session.sent)
        await dp.feed_update(bot, Update(update_id=2, callback_query=callback))
        audience_data = offer.reply_markup.inline_keyboard[1][0].callback_data
        audience_before = len(session.sent)
        await dp.feed_update(
            bot,
            Update(
                update_id=3,
                callback_query=callback.model_copy(
                    update={"id": "audience", "data": audience_data}
                ),
            ),
        )
        await runtime.jobs.close()

    details = "\n".join(
        item.text for item in session.sent[before:] if isinstance(item, SendMessage) and item.text
    )
    assert "Ключевые показатели:" in details
    assert "Следующий шаг:" in details
    audience = "\n".join(
        item.text
        for item in session.sent[audience_before:]
        if isinstance(item, (SendMessage, EditMessageText)) and item.text
    )
    assert "Аудитория" in audience
    assert "Рекламный трафик Директа:" in audience


async def test_unknown_chats_silent(runtime):
    session = FakeTelegram()
    async with Bot(token="555:THIS_IS_A_SYNTHETIC_TEST_TOKEN", session=session) as bot:
        await build_dispatcher(runtime).feed_update(bot, message_update("/clients", chat=999))
    assert not session.sent


async def test_question_continues_when_initial_status_delivery_fails(runtime):
    class FlakyTelegram(FakeTelegram):
        failed = False

        async def make_request(self, bot, method, timeout=None):  # noqa: ASYNC109
            if isinstance(method, SendMessage) and not self.failed:
                self.failed = True
                raise TelegramNetworkError(method=method, message="Request timeout error")
            return await super().make_request(bot, method, timeout)

    session = FlakyTelegram()
    runtime.agent.ask = AsyncMock(return_value="Готовый анализ")
    async with Bot(token="555:THIS_IS_A_SYNTHETIC_TEST_TOKEN", session=session) as bot:
        await build_dispatcher(runtime).feed_update(bot, message_update("Проверь клиента"))
        await runtime.jobs.close()
    runtime.agent.ask.assert_awaited_once()
    assert any(isinstance(m, SendMessage) and m.text == "Готовый анализ" for m in session.sent)
    assert not runtime.jobs.tasks


@pytest.mark.parametrize("all_clients", [False, True])
@pytest.mark.parametrize("ack_failure", [False, True])
async def test_menu_report_flow_and_replay(runtime, all_clients, ack_failure):
    from aiogram.types import CallbackQuery

    class CallbackTimeoutTelegram(FakeTelegram):
        async def make_request(self, bot, method, timeout=None):  # noqa: ASYNC109
            if ack_failure and isinstance(method, AnswerCallbackQuery):
                self.sent.append(method)
                raise TelegramNetworkError(method=method, message="Request timeout error")
            return await super().make_request(bot, method, timeout)

    session = CallbackTimeoutTelegram()
    spy = AsyncMock(wraps=runtime.checks.run_check)
    runtime.checks.run_check = spy
    async with Bot(token="555:THIS_IS_A_SYNTHETIC_TEST_TOKEN", session=session) as bot:
        dp = build_dispatcher(runtime)

        async def click(label, user=1, data=None):
            if data is None:
                keyboard = session.sent[-1].reply_markup.inline_keyboard
                data = next(b.callback_data for row in keyboard for b in row if label in b.text)
            cb = CallbackQuery(
                id="nav",
                from_user=User(id=user, is_bot=False, first_name="U"),
                chat_instance="test",
                message=message_update("menu").message,
                data=data,
            )
            await dp.feed_update(bot, Update(update_id=2, callback_query=cb))
            return data

        await dp.feed_update(bot, message_update("/menu"))
        if all_clients:
            await click("Аналитика всех")
        else:
            await click("Клиенты")
            await click("West")
        await click("Краткая")
        token = session.sent[-1].reply_markup.inline_keyboard[2][0].callback_data
        await click("", user=2, data=token)
        assert next(
            m for m in reversed(session.sent) if isinstance(m, AnswerCallbackQuery)
        ).show_alert
        assert not spy.called
        await click("", data=token)
        await runtime.jobs.close()
        assert spy.call_count == 1
        assert spy.call_args.args[1].current.days == 14
        assert spy.call_args.args[2] == "summary"
        assert len(spy.call_args.args[0]) == (3 if all_clients else 1)
        await click("", data=token)
        assert next(
            m for m in reversed(session.sent) if isinstance(m, AnswerCallbackQuery)
        ).show_alert
        assert spy.call_count == 1


async def test_menu_command_retries_network_failure(runtime):
    class FlakyMenuTelegram(FakeTelegram):
        failures = 0

        async def make_request(self, bot, method, timeout=None):  # noqa: ASYNC109
            if isinstance(method, SendMessage) and self.failures == 0:
                self.failures += 1
                raise TelegramNetworkError(method=method, message="Request timeout error")
            return await super().make_request(bot, method, timeout)

    session = FlakyMenuTelegram()
    async with Bot(token="555:THIS_IS_A_SYNTHETIC_TEST_TOKEN", session=session) as bot:
        await build_dispatcher(runtime).feed_update(bot, message_update("/menu"))
    assert session.failures == 1
    assert session.sent[-1].reply_markup.inline_keyboard


async def test_menu_pagination_and_cancel(runtime):
    from aiogram.types import CallbackQuery

    template = next(iter(runtime.registry.clients.values()))
    runtime.registry.clients = {
        str(i): template.model_copy(update={"id": str(i), "name": f"Client {i}"}) for i in range(19)
    }
    session = FakeTelegram()
    async with Bot(token="555:THIS_IS_A_SYNTHETIC_TEST_TOKEN", session=session) as bot:
        dp = build_dispatcher(runtime)
        await dp.feed_update(bot, message_update("/clients"))
        assert "Страница 1 из 3" in session.sent[-1].text
        keyboard = session.sent[-1].reply_markup.inline_keyboard
        assert sum(b.text.startswith("Client") for row in keyboard for b in row) == 8
        token = next(b.callback_data for row in keyboard for b in row if "Вперёд" in b.text)
        cb = CallbackQuery(
            id="page",
            from_user=User(id=1, is_bot=False, first_name="U"),
            chat_instance="test",
            message=message_update("menu").message,
            data=token,
        )
        await dp.feed_update(bot, Update(update_id=2, callback_query=cb))
        assert "Страница 2 из 3" in session.sent[-1].text
        assert session.sent[-1].reply_markup.inline_keyboard[0][0].text == "Client 8"
        token = session.sent[-1].reply_markup.inline_keyboard[0][0].callback_data
        await dp.feed_update(bot, message_update("/cancel"))
        await dp.feed_update(
            bot, Update(update_id=3, callback_query=cb.model_copy(update={"data": token}))
        )
        assert session.sent[-1].show_alert


async def test_inline_selection_preserves_mode_period_and_owner(runtime):
    session = FakeTelegram()
    async with Bot(token="555:THIS_IS_A_SYNTHETIC_TEST_TOKEN", session=session) as bot:
        dp = build_dispatcher(runtime)
        await dp.feed_update(bot, message_update("/summary 14d"))
        buttons = session.sent[-1].reply_markup
        data = buttons.inline_keyboard[0][0].callback_data
        from aiogram.types import CallbackQuery

        callback = CallbackQuery(
            id="callback",
            from_user=User(id=2, is_bot=False, first_name="Other"),
            chat_instance="test",
            message=message_update("Choose").message,
            data=data,
        )
        await dp.feed_update(bot, Update(update_id=2, callback_query=callback))
        assert session.sent[-1].show_alert
        callback = callback.model_copy(
            update={"from_user": callback.from_user.model_copy(update={"id": 1})}
        )
        spy = AsyncMock(wraps=runtime.checks.run_check)
        runtime.checks.run_check = spy
        await dp.feed_update(bot, Update(update_id=3, callback_query=callback))
        await runtime.jobs.close()
        assert spy.call_args.args[1].current.days == 14
        assert spy.call_args.args[2] == "summary"


async def test_schedule_admin_and_10am_timezone(runtime):
    runtime.settings.schedule_enabled = True
    schedule = DailySchedule(runtime.settings, runtime.checks, AsyncMock())
    runtime.schedule = schedule
    schedule.start()
    job = schedule.scheduler.get_job("daily")
    assert str(job.trigger.timezone) == "Europe/Moscow"
    assert job.next_run_time.hour == 10 and job.next_run_time.minute == 0
    session = FakeTelegram()
    async with Bot(token="555:THIS_IS_A_SYNTHETIC_TEST_TOKEN", session=session) as bot:
        dp = build_dispatcher(runtime)
        await dp.feed_update(bot, message_update("/schedule", user=2))
        assert "TELEGRAM_ADMIN_USER_IDS" in session.sent[-1].text
        await dp.feed_update(bot, message_update("/schedule", user=1))
        assert "10:00 Europe/Moscow" in session.sent[-1].text


async def test_daily_shared_service_and_idempotency_across_restart(runtime):
    send = AsyncMock()
    spy = AsyncMock(wraps=runtime.checks.run_check)
    runtime.checks.run_check = spy
    await DailySchedule(runtime.settings, runtime.checks, send).run()
    count = send.await_count
    assert count > 0
    assert [call.args[1].current.days for call in spy.call_args_list] == [1, 7]
    assert all(call.args[3] == "schedule" for call in spy.call_args_list)
    digest = "\n".join(call.args[1] for call in send.await_args_list)
    assert "Ежедневный контроль рекламы" in digest
    assert "Вчера" in digest and "7 дней" in digest
    assert "Ежедневная проверка: вчера и последние 7" not in digest
    assert len(digest) < 4000
    await DailySchedule(runtime.settings, runtime.checks, send).run()
    assert send.await_count == count
    assert spy.await_count == 2


async def test_daily_model_gets_one_compact_digest(runtime):
    send = AsyncMock()
    agent = AsyncMock()
    agent.explain_daily_digest.side_effect = lambda text, chat: (text, False)
    await DailySchedule(runtime.settings, runtime.checks, send, agent).run()
    agent.explain_daily_digest.assert_awaited_once()
    assert "Ежедневный контроль рекламы" in agent.explain_daily_digest.await_args.args[0]


async def test_failed_delivery_resumes_without_rerunning_analysis(runtime):
    send = AsyncMock(side_effect=RuntimeError("telegram down"))
    spy = AsyncMock(wraps=runtime.checks.run_check)
    runtime.checks.run_check = spy
    await DailySchedule(runtime.settings, runtime.checks, send).run()
    send.side_effect = None
    await DailySchedule(runtime.settings, runtime.checks, send).run()
    assert spy.await_count == 2
    assert send.await_count >= 2


async def test_background_work_does_not_block_other_messages(runtime):
    jobs = BackgroundJobs(2)
    started, release = asyncio.Event(), asyncio.Event()

    async def work():
        started.set()
        await release.wait()

    assert jobs.start((1, 1), work, AsyncMock())
    await started.wait()
    assert not jobs.start((1, 1), work, AsyncMock())
    ran = asyncio.Event()

    async def other():
        ran.set()

    assert jobs.start((1, 2), other, AsyncMock())
    await asyncio.wait_for(ran.wait(), 1)
    release.set()
    await jobs.close()
