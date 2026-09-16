import asyncio
from unittest.mock import AsyncMock, MagicMock

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
    monkeypatch.setattr(main, "DailySchedule", MagicMock())
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
