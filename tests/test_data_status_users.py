from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from aiogram import Bot
from sqlalchemy import update

from app.analytics.periods import DateRange, make_period
from app.bot.handlers import build_dispatcher
from app.bot.middleware import AccessMiddleware
from app.reporting.data_status import describe_data
from app.storage.models import DailySnapshot
from app.storage.repository import Repository
from tests.test_bot_schedule import FakeTelegram, message_update


async def test_status_counts_stale_missing_and_incomplete_pages(runtime, client):
    repo = runtime.checks.repository
    yesterday = make_period("yesterday").current
    snapshot = await runtime.checks.provider.snapshot(client, yesterday)
    await repo.save_snapshot(client.id, yesterday, snapshot)
    await repo.save_daily_snapshot(client.id, yesterday, snapshot)
    old_day = yesterday.start - timedelta(days=1)
    old_period = DateRange(start=old_day, end=old_day)
    await repo.save_daily_snapshot(client.id, old_period, snapshot)
    async with repo.sessions.begin() as s:
        await s.execute(
            update(DailySnapshot)
            .where(DailySnapshot.day == str(old_day))
            .values(refresh_after=datetime.now(UTC) - timedelta(hours=1))
        )
    await repo.save_dimension_page(client.id, yesterday.start, "search", 1, [], last_page=True)
    text = await describe_data(runtime, 123456789, client.id)
    assert "свежие данные за 1 из 30" in text
    assert "не загружено 28" in text
    assert "Запросы: 0/30; начато, но не завершено: 1" in text
    assert "Последний сохранённый срез" in text
    await repo.save_dimension_page(client.id, yesterday.start, "search", 0, [], last_page=False)
    assert "Запросы: 1/30" in await describe_data(runtime, 123456789, client.id)
    assert "Сохранённых срезов пока нет" in await describe_data(runtime, 123456789, "fresh_parfum")


async def test_persisted_user_access_and_revocation(runtime, client):
    repo = runtime.checks.repository
    await repo.set_bot_user(42, True, [client.id], 1)
    # A fresh repository reads the grant: it does not depend on in-memory settings.
    restored = Repository(repo.sessions, repo.app_mode)
    middleware = AccessMiddleware(
        [123456789], [1], repository=restored, registry=runtime.registry, admins=[1]
    )
    handler = AsyncMock()
    event = message_update("/clients", chat=42, user=42).message
    await middleware(handler, event, {})
    handler.assert_awaited_once()
    assert [c.id for c in runtime.registry.visible(42)] == [client.id]
    assert runtime.registry.visible(43) == []
    await repo.set_bot_user(42, False, [], 1)
    handler.reset_mock()
    await middleware(handler, event, {})
    await middleware(handler, message_update("/menu", chat=123456789, user=42).message, {})
    handler.assert_not_awaited()
    assert runtime.registry.visible(42) == []


async def test_user_menu_add_and_status_ui(runtime):
    from aiogram.types import CallbackQuery, Update, User

    session = FakeTelegram()
    async with Bot(token="555:THIS_IS_A_SYNTHETIC_TEST_TOKEN", session=session) as bot:
        dp = build_dispatcher(runtime)

        async def click(label, user=1):
            data = next(
                b.callback_data
                for row in session.sent[-1].reply_markup.inline_keyboard
                for b in row
                if label in b.text
            )
            await dp.feed_update(
                bot,
                Update(
                    update_id=2,
                    callback_query=CallbackQuery(
                        id="nav",
                        from_user=User(id=user, is_bot=False, first_name="U"),
                        chat_instance="test",
                        message=message_update("menu").message,
                        data=data,
                    ),
                ),
            )

        await dp.feed_update(bot, message_update("/menu"))
        await click("Пользователи")
        await click("Добавить по")
        await dp.feed_update(bot, message_update("42"))
        assert (await runtime.checks.repository.bot_users())[42]["enabled"]
        await click("Удалить 42")
        assert not (await runtime.checks.repository.bot_users())[42]["enabled"]
        await click("Главное меню")
        await click("Клиенты")
        await click("West")
        await click("Состояние данных")
        assert "Дневное хранилище" in session.sent[-1].text
        assert "Сохранённых срезов пока нет" in session.sent[-1].text
