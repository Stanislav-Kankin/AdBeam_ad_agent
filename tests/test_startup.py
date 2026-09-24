import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramNetworkError, TelegramUnauthorizedError
from aiogram.methods import GetMe
from aiogram.types import User

from app import main


async def test_polling_starts_while_command_registration_stalls(runtime, monkeypatch):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def stall(*args, **kwargs):
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    bot = AsyncMock()
    bot.set_my_commands.side_effect = stall
    bot.__aenter__.return_value = bot
    monkeypatch.setattr(main, "Bot", MagicMock(return_value=bot))
    schedule = MagicMock()
    schedule.return_value.close = AsyncMock()
    monkeypatch.setattr(main, "DailySchedule", schedule)
    monkeypatch.setattr(main, "heartbeat", stall)
    dp = MagicMock()

    async def poll(*args, **kwargs):
        await asyncio.wait_for(started.wait(), 1)
        assert not cancelled.is_set()

    dp.start_polling = AsyncMock(side_effect=poll)
    dp.resolve_used_update_types.return_value = ["message"]
    monkeypatch.setattr(main, "build_dispatcher", MagicMock(return_value=dp))
    from pydantic import SecretStr

    runtime.settings.telegram_bot_token = SecretStr("555:synthetic")
    await asyncio.wait_for(main.run_bot(runtime), 2)
    dp.start_polling.assert_awaited_once()
    bot.set_my_commands.assert_awaited_once()
    assert cancelled.is_set()


async def test_command_registration_retries_timeout(monkeypatch):
    bot = AsyncMock()
    bot.set_my_commands.side_effect = [TimeoutError(), True]
    monkeypatch.setattr(main.asyncio, "sleep", AsyncMock())
    await main.register_commands(bot, [("menu", "Меню")])
    assert bot.set_my_commands.await_count == 2


async def test_real_bot_getme_retries_and_caches(monkeypatch):
    # Use the real Bot.me implementation, including its cache used by polling.
    bot = Bot("555:synthetic")
    get_me = AsyncMock(
        side_effect=[
            TelegramNetworkError(method=GetMe(), message="Request timeout error"),
            TimeoutError(),
            User(id=555, is_bot=True, first_name="Test", username="test_bot"),
        ]
    )
    monkeypatch.setattr(bot, "get_me", get_me)
    sleep = AsyncMock()
    monkeypatch.setattr(main.asyncio, "sleep", sleep)
    try:
        await main.wait_for_telegram(bot)
        assert (await bot.me()).id == 555
        assert get_me.await_count == 3
        assert [c.args[0] for c in sleep.call_args_list] == [2, 4]
    finally:
        await bot.session.close()


async def test_getme_retry_can_be_cancelled():
    bot = AsyncMock()
    entered = asyncio.Event()

    async def stall():
        entered.set()
        await asyncio.Future()

    bot.me.side_effect = stall
    task = asyncio.create_task(main.wait_for_telegram(bot))
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_invalid_token_is_not_retried_forever():
    bot = AsyncMock()
    bot.me.side_effect = TelegramUnauthorizedError(method=GetMe(), message="Unauthorized")
    with pytest.raises(TelegramUnauthorizedError):
        await main.wait_for_telegram(bot)
    bot.me.assert_awaited_once()
