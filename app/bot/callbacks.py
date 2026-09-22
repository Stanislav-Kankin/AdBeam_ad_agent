"""Callback acknowledgements must not prevent the requested action."""

import asyncio
import logging

from aiogram.exceptions import TelegramAPIError

logger = logging.getLogger(__name__)


async def answer_callback(callback, text=None, *, show_alert=False):
    try:
        async with asyncio.timeout(3):
            await callback.answer(text, show_alert=show_alert, request_timeout=3)
        return True
    except (TelegramAPIError, TimeoutError) as exc:
        logger.warning("Telegram callback acknowledgement failed: %s", type(exc).__name__)
        return False
