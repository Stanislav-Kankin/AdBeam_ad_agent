import asyncio
import re
import secrets
from time import monotonic

from aiogram import Dispatcher, F, Router
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.analytics.periods import make_period
from app.analytics.progress import progress_state
from app.bot.commands import HELP, parse_command
from app.bot.markdown import markdown_parts
from app.bot.menu import install_menu
from app.bot.middleware import AccessMiddleware
from app.bot.report_message import ReportMessage, report_entities
from app.domain.reports import CheckMode, TriggerSource
from app.reporting.formatter import split_message


async def send_text(bot, chat_id, text, *, markdown=False):
    if markdown:
        for part, entities in markdown_parts(text):
            await send_part(bot, chat_id, part, entities=entities)
        return
    for part in split_message(text):
        await send_part(bot, chat_id, part)


async def send_part(bot, chat_id, text, entities=None):
    for attempt in range(3):
        try:
            return await bot.send_message(
                chat_id,
                text,
                parse_mode=None,
                entities=report_entities(text) if entities is None else entities,
            )
        except TelegramRetryAfter as exc:
            if attempt == 2 or exc.retry_after > 60:
                raise
            await asyncio.sleep(exc.retry_after)


def build_dispatcher(runtime):
    dp, router = Dispatcher(), Router()
    middleware = AccessMiddleware(
        runtime.settings.telegram_allowed_chat_ids, runtime.settings.telegram_allowed_user_ids
    )
    router.message.outer_middleware(middleware)
    router.callback_query.outer_middleware(middleware)
    pending = {}

    async def launch(message, user_id, client_ids, period, mode):
        presentation = ReportMessage(message)

        async def work():
            state = {"stage": "ожидаю свободного места в очереди"}
            token = progress_state.set(state)
            try:
                await presentation.start(state)
                _, text = await runtime.checks.run_check(
                    client_ids,
                    make_period(period),
                    mode,
                    TriggerSource.TELEGRAM,
                    chat_id=message.chat.id,
                    user_id=user_id,
                )
                state["stage"] = "отправляю результат"
                await presentation.finish(text)
            finally:
                await presentation.stop()
                progress_state.reset(token)

        async def failed():
            await presentation.finish(
                "Проверка не завершилась. Попробуйте позже; ошибка записана в журнал."
            )

        if not runtime.jobs.start((message.chat.id, user_id), work, failed):
            await message.answer(
                "Проверка уже выполняется или все рабочие слоты заняты. Попробуйте чуть позже."
            )

    show_menu, clear_menu = install_menu(router, runtime, launch)

    async def select_client(message, clients, command):
        expired = [k for k, v in pending.items() if monotonic() - v[0] > 600]
        for key in expired:
            pending.pop(key)
        if len(pending) >= 200:
            pending.pop(next(iter(pending)))
        nonce = secrets.token_hex(6)
        ids = [c.id for c in clients]
        pending[nonce] = (monotonic(), message.chat.id, message.from_user.id, ids, command)
        # Paginate by separate messages, keeping callback data below 64 bytes.
        for offset in range(0, len(clients), 30):
            buttons = [
                [InlineKeyboardButton(text=c.name, callback_data=f"pick:{nonce}:{offset + i}")]
                for i, c in enumerate(clients[offset : offset + 30])
            ]
            await message.answer(
                "Выберите клиента:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
            )

    @router.callback_query(F.data.startswith("pick:"))
    async def choose(callback):
        try:
            _, nonce, index = callback.data.split(":")
            created, chat_id, user_id, ids, command = pending[nonce]
            if (
                monotonic() - created > 600
                or chat_id != callback.message.chat.id
                or user_id != callback.from_user.id
            ):
                raise ValueError
            i = int(index)
            if not 0 <= i < len(ids):
                raise ValueError
            client = runtime.registry.require(chat_id, ids[i])
        except (ValueError, KeyError, PermissionError):
            await callback.answer(
                "Выбор устарел или предназначен другому пользователю.", show_alert=True
            )
            return
        pending.pop(nonce)
        await callback.answer()
        await callback.message.edit_reply_markup(reply_markup=None)
        await launch(
            callback.message,
            user_id,
            [client.id],
            command.period,
            CheckMode.SUMMARY if command.name == "summary" else CheckMode.STANDARD,
        )

    @router.message(F.text.startswith("/"))
    async def command_handler(message):
        prefix = message.text.split()[0]
        if "@" in prefix:
            me = await message.bot.me()
            if prefix.split("@", 1)[1].casefold() != me.username.casefold():
                return
        name = prefix.split("@", 1)[0][1:]
        if name in ("start", "menu"):
            await show_menu(message, message.from_user.id)
        elif name == "help":
            await message.answer(HELP)
        elif name == "clients":
            await show_menu(message, message.from_user.id, screen="clients")
        elif name == "cancel":
            clear_menu(message.chat.id, message.from_user.id)
            for key in [
                k for k, v in pending.items() if v[1:3] == (message.chat.id, message.from_user.id)
            ]:
                pending.pop(key)
            runtime.agent.cancel(message.chat.id, message.from_user.id)
            await message.answer(
                "Выбор отменён, контекст диалога очищен. Уже запущенные проверки продолжатся."
            )
        elif name == "schedule":
            if message.from_user.id not in runtime.settings.telegram_admin_user_ids:
                await message.answer("Команда доступна пользователям из TELEGRAM_ADMIN_USER_IDS.")
                return
            await message.answer(await runtime.schedule.describe())
        elif name in ("check", "check_all", "summary", "summary_all"):
            try:
                cmd = parse_command(message.text)
            except ValueError as exc:
                await message.answer(str(exc))
                return
            clients = (
                runtime.registry.visible(message.chat.id)
                if name.endswith("_all") or not cmd.client_query
                else runtime.registry.resolve(message.chat.id, cmd.client_query)
            )
            if not clients:
                await message.answer(
                    "Клиент не найден или недоступен этому чату. /clients — доступные клиенты."
                )
            elif not name.endswith("_all") and (not cmd.client_query or len(clients) != 1):
                await select_client(message, clients, cmd)
            else:
                await launch(
                    message,
                    message.from_user.id,
                    [c.id for c in clients],
                    cmd.period,
                    CheckMode.SUMMARY if name.startswith("summary") else CheckMode.STANDARD,
                )
        else:
            await message.answer("Неизвестная команда. /help — список команд.")

    @router.message(F.text)
    async def question(message):
        if message.chat.type != "private":
            me = await message.bot.me()
            reply = message.reply_to_message
            replied = bool(reply and reply.from_user and reply.from_user.id == me.id)
            mentioned = bool(
                me.username
                and re.search(
                    r"(?<!\w)@" + re.escape(me.username) + r"(?!\w)", message.text, re.IGNORECASE
                )
            )
            if not replied and not mentioned:
                return

        async def work():
            await message.answer("Разбираю вопрос и проверяю данные.")
            answer = await runtime.agent.ask(message.text, message.chat.id, message.from_user.id)
            await send_text(message.bot, message.chat.id, answer, markdown=True)

        async def failed():
            await message.answer("Не удалось обработать вопрос. Попробуйте /check <клиент>.")

        if not runtime.jobs.start((message.chat.id, message.from_user.id), work, failed):
            await message.answer("Ваш запрос уже выполняется или все рабочие слоты заняты.")

    dp.include_router(router)
    return dp
