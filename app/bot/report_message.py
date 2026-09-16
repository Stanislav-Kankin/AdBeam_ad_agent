"""Telegram presentation without interpreting client data as markup."""

import asyncio
import logging
from time import monotonic

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import MessageEntity

from app.reporting.formatter import METRIC_NAMES, split_message

logger = logging.getLogger(__name__)
FRAMES = ("● ○ ○", "○ ● ○", "○ ○ ●", "○ ● ○")
PROGRESS_INTERVAL = 4


def report_entities(text):
    entities, offset = [], 0
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        if (
            any(content.startswith(label + ":") for label in METRIC_NAMES.values())
            or content.startswith(("📊 AdBeam", "🧪 MOCK", "🔴 ", "🟡 ", "🟢 ", "⚪ "))
            or content
            in (
                "Ключевые показатели:",
                "Что известно:",
                "Цели Метрики:",
                "Следующий шаг:",
                "Ограничения анализа:",
                "Источники данных:",
                "Что изменилось — три главных вывода:",
                "Что рекомендуется проверить:",
            )
        ):
            entities.append(
                MessageEntity(
                    type="bold", offset=offset, length=len(content.encode("utf-16-le")) // 2
                )
            )
        offset += len(line.encode("utf-16-le")) // 2
    return entities


async def retry_telegram(call):
    for attempt in range(3):
        try:
            return await call()
        except TelegramRetryAfter as exc:
            if attempt == 2 or exc.retry_after > 60:
                raise
            await asyncio.sleep(exc.retry_after)


class ReportMessage:
    def __init__(self, message):
        self.message = message
        self.status = None
        self.task = None

    async def start(self, state):
        self.status = await self.message.answer(
            "● ○ ○  Проверка началась. Пришлю результат сюда.", parse_mode=None
        )
        self.status.as_(self.message.bot)
        self.task = asyncio.create_task(self.animate(state))

    async def animate(self, state):
        started, frame = monotonic(), 0
        while True:
            await asyncio.sleep(PROGRESS_INTERVAL)
            frame += 1
            text = f"{FRAMES[frame % len(FRAMES)]}  Проверка выполняется · {int(monotonic() - started)} с\n\n{state['stage']}"
            try:
                async with asyncio.timeout(7):
                    await self.status.edit_text(text, parse_mode=None)
            except TelegramRetryAfter as exc:
                await asyncio.sleep(min(exc.retry_after, 60))
            except Exception:
                logger.warning("Could not update check progress")

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def finish(self, text):
        # Stop and await animation before editing: late progress cannot overwrite the report.
        await self.stop()
        parts = split_message(text)
        if not parts:
            return
        if self.status:
            try:
                await retry_telegram(
                    lambda: self.status.edit_text(
                        parts[0], parse_mode=None, entities=report_entities(parts[0])
                    )
                )
                parts = parts[1:]
            except TelegramBadRequest:
                # The user may have deleted the progress message.
                logger.info("Progress message could not be edited; sending report separately")
        for part in parts:
            await retry_telegram(
                lambda part=part: self.message.answer(
                    part, parse_mode=None, entities=report_entities(part)
                )
            )
