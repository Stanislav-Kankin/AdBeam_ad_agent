"""Telegram presentation without interpreting client data as markup."""

import asyncio
import logging
from time import monotonic

from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.types import MessageEntity

from app.bot.markdown import markdown_parts
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
            or content.startswith(
                (
                    "Визиты сайта:",
                    "Посетители сайта:",
                    "Просмотры страниц:",
                    "Отказы, %:",
                    "Глубина просмотра:",
                    "Среднее время на сайте, сек.:",
                )
            )
            or content.startswith(("📊 AdBeam", "🧪 MOCK", "🔴 ", "🟡 ", "🟢 ", "⚪ "))
            or content
            in (
                "Ключевые показатели:",
                "Динамика показателей:",
                "Основное изменение:",
                "Статус:",
                "Что известно:",
                "Цели Метрики:",
                "Поведение на сайте (весь выбранный счётчик):",
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
        except (TelegramNetworkError, TimeoutError):
            if attempt == 2:
                raise
            logger.warning("Telegram delivery retry attempt=%s", attempt + 2)
            await asyncio.sleep(1 + attempt)


class ReportMessage:
    def __init__(self, message):
        self.message = message
        self.status = None
        self.task = None

    async def start(self, state):
        await self.send_status()
        self.task = asyncio.create_task(self.animate(state))

    async def send_status(self):
        try:
            self.status = await self.message.answer(
                "● ○ ○  Проверка началась. Пришлю результат сюда.",
                parse_mode=None,
                request_timeout=7,
            )
            self.status.as_(self.message.bot)
        except (TelegramNetworkError, TimeoutError):
            logger.warning("Progress delivery failed; analysis continues")

    async def animate(self, state):
        started, frame = monotonic(), 0
        while True:
            await asyncio.sleep(PROGRESS_INTERVAL)
            frame += 1
            text = f"{FRAMES[frame % len(FRAMES)]}  Проверка выполняется · {int(monotonic() - started)} с\n\n{state['stage']}"
            try:
                if self.status is None:
                    await self.send_status()
                    continue
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

    async def finish(self, text, *, markdown=False):
        # Stop and await animation before editing: late progress cannot overwrite the report.
        await self.stop()
        parts = (
            list(markdown_parts(text))
            if markdown
            else [(part, report_entities(part)) for part in split_message(text)]
        )
        if not parts:
            return
        if self.status:
            try:
                await retry_telegram(
                    lambda: self.status.edit_text(
                        parts[0][0], parse_mode=None, entities=parts[0][1]
                    )
                )
                parts = parts[1:]
            except TelegramBadRequest:
                # The user may have deleted the progress message.
                logger.info("Progress message could not be edited; sending report separately")
        for part, entities in parts:
            await retry_telegram(
                lambda part=part, entities=entities: self.message.answer(
                    part, parse_mode=None, entities=entities
                )
            )
