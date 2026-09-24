import asyncio
import logging
import re
import secrets
from time import monotonic

from aiogram import Dispatcher, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import BufferedInputFile, InlineKeyboardButton, InlineKeyboardMarkup

from app.analytics.periods import AnalysisPeriod, make_period
from app.analytics.progress import progress_state
from app.bot.callbacks import answer_callback
from app.bot.commands import HELP, parse_command
from app.bot.markdown import markdown_parts
from app.bot.menu import install_menu
from app.bot.middleware import AccessMiddleware
from app.bot.report_message import ReportMessage, report_entities, retry_telegram
from app.domain.reports import CheckMode, ClientReport, TriggerSource
from app.reporting.charts import render_dynamics
from app.reporting.formatter import audience_report, campaigns_view, detailed, split_message

logger = logging.getLogger(__name__)

ANALYTICS_WORDS = re.compile(
    r"анализ|аналитик|обзор|отч[её]т|динамик|показател|расход|клик|конверс|"
    r"трафик|кампан|директ|метрик|\bcpa\b|\bctr\b|\bcpc\b|\bcr\b|дрр",
    re.IGNORECASE,
)
ALL_CLIENTS_WORDS = re.compile(r"все\s+клиент|по\s+всем\s+клиент|всех\s+клиент", re.IGNORECASE)


async def send_text(bot, chat_id, text, *, markdown=False):
    if markdown:
        for part, entities in markdown_parts(text):
            await send_part(bot, chat_id, part, entities=entities)
        return
    for part in split_message(text):
        await send_part(bot, chat_id, part)


async def send_part(bot, chat_id, text, entities=None):
    return await retry_telegram(
        lambda: bot.send_message(
            chat_id,
            text,
            parse_mode=None,
            entities=report_entities(text) if entities is None else entities,
            request_timeout=15,
        )
    )


def build_dispatcher(runtime):
    dp, router = Dispatcher(), Router()
    middleware = AccessMiddleware(
        runtime.settings.telegram_allowed_chat_ids,
        runtime.settings.telegram_allowed_user_ids,
        repository=runtime.checks.repository,
        registry=runtime.registry,
        admins=runtime.settings.telegram_admin_user_ids,
    )
    router.message.outer_middleware(middleware)
    router.callback_query.outer_middleware(middleware)
    pending = {}
    report_details = {}

    def expire_details():
        expired = [key for key, value in report_details.items() if monotonic() - value[0] > 900]
        for key in expired:
            report_details.pop(key, None)

    async def offer_details(message, reports, user_id):
        if len(reports) != 1:
            return
        expire_details()
        if len(report_details) >= 200:
            report_details.pop(next(iter(report_details)))
        token = secrets.token_hex(6)
        report_details[token] = (
            monotonic(),
            message.chat.id,
            user_id,
            campaigns_view(reports[0]),
            detailed(reports[0]),
            reports[0].client_id,
            reports[0].period.model_dump(mode="json"),
        )
        try:
            await retry_telegram(
                lambda: message.answer(
                    "Дополнительные данные",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="📈 Кампании",
                                    callback_data=f"details:{token}:specialist",
                                )
                            ],
                            [
                                InlineKeyboardButton(
                                    text="👥 Аудитория",
                                    callback_data=f"details:{token}:audience",
                                )
                            ],
                            [
                                InlineKeyboardButton(
                                    text="⚙️ Технические данные",
                                    callback_data=f"details:{token}:technical",
                                )
                            ],
                        ]
                    ),
                    request_timeout=15,
                )
            )
        except (TelegramAPIError, TimeoutError) as exc:
            report_details.pop(token, None)
            logger.warning("Could not offer report details: %s", type(exc).__name__)

    @router.callback_query(F.data.startswith("details:"))
    async def show_details(callback):
        expire_details()
        try:
            _, token, view = callback.data.split(":", 2)
            created, chat_id, user_id, specialist, technical, client_id, period_data = (
                report_details[token]
            )
            if (
                monotonic() - created > 900
                or chat_id != callback.message.chat.id
                or user_id != callback.from_user.id
                or view not in ("specialist", "technical", "audience")
            ):
                raise ValueError
        except (KeyError, ValueError):
            await answer_callback(
                callback, "Расшифровка устарела. Запустите проверку ещё раз.", show_alert=True
            )
            return
        await answer_callback(callback)
        if view == "audience":
            presentation = ReportMessage(callback.message)

            async def work():
                state = {"stage": "загружаю аудиторию Директа и интересы Метрики"}
                token = progress_state.set(state)
                try:
                    await presentation.start(state)
                    client = runtime.registry.require(chat_id, client_id)
                    period = AnalysisPeriod.model_validate(period_data)
                    payload = await runtime.checks.audience(client, period)
                    await presentation.finish(audience_report(client_label(client), payload))
                finally:
                    await presentation.stop()
                    progress_state.reset(token)

            async def failed():
                await presentation.finish(
                    "Не удалось загрузить аудиторные срезы. Ошибка записана в журнал."
                )

            if not runtime.jobs.start((chat_id, user_id), work, failed):
                await callback.message.answer("Другой запрос уже выполняется. Попробуйте позже.")
            return
        # The campaigns view is Markdown (bold names); the technical view is plain text.
        if view == "specialist":
            await send_text(callback.message.bot, chat_id, specialist, markdown=True)
        else:
            await send_text(callback.message.bot, chat_id, technical)

    def client_label(client):
        name = client.name.strip()
        login = client.direct.client_login.strip()
        return name if login.casefold() in name.casefold() else f"{name} · {login}"

    def mentioned_clients(text, chat_id):
        folded = text.casefold()
        found = []
        for client in runtime.registry.visible(chat_id):
            references = [client.name, client.direct.client_login, *client.aliases]
            if any(len(ref.strip()) >= 3 and ref.casefold() in folded for ref in references):
                found.append(client.id)
        return set(found)

    async def send_chart(message, client, period, data=None):
        current, previous = data or await runtime.checks.dynamics(client, period)
        usable = all(
            result.status.value in ("ok", "no_data")
            or (result.status.value == "insufficient" and bool(result.rows))
            for result in (current, previous)
        )
        if not usable:
            return False
        label = client_label(client)
        image = await asyncio.to_thread(render_dynamics, label, period, current, previous)
        partial = any(result.status.value == "insufficient" for result in (current, previous))
        caption = (
            f"📈 {label} · {period.current.label()}\n"
            f"Сравнение: {period.previous.label()}\n"
            "Сплошная линия — текущий период; пунктир — предыдущий. Источник: Директ."
        )
        if partial:
            caption += "\n⚠️ Часть дневных данных ограничена ответом Директа."
        await retry_telegram(
            lambda: message.bot.send_photo(
                message.chat.id,
                BufferedInputFile(image, filename="adbeam-dynamics.png"),
                caption=caption,
                request_timeout=30,
            )
        )
        return True

    async def launch(message, user_id, client_ids, period, mode):
        presentation = ReportMessage(message)

        async def work():
            state = {"stage": "ожидаю свободного места в очереди"}
            token = progress_state.set(state)
            chart_task = None
            try:
                await presentation.start(state)
                analysis_period = make_period(period)
                reports, text = await runtime.checks.run_check(
                    client_ids,
                    analysis_period,
                    mode,
                    TriggerSource.TELEGRAM,
                    chat_id=message.chat.id,
                    user_id=user_id,
                )
                if len(client_ids) == 1:
                    client = runtime.registry.require(message.chat.id, client_ids[0])
                    chart_task = asyncio.create_task(
                        runtime.checks.dynamics(client, analysis_period)
                    )
                    await runtime.checks.repository.save_conversation(
                        message.chat.id,
                        user_id,
                        None,
                        active_client_id=client_ids[0],
                        period=reports[0].period.model_dump(mode="json") if reports else None,
                    )
                state["stage"] = "формулирую выводы"
                text, markdown = await runtime.agent.explain_reports(
                    reports, text, message.chat.id, user_id
                )
                state["stage"] = "отправляю результат"
                await presentation.finish(text, markdown=markdown)
                await offer_details(message, reports, user_id)
                if chart_task:
                    try:
                        if not await send_chart(message, client, analysis_period, await chart_task):
                            await message.answer(
                                "Текстовый анализ готов, но дневные данные для графика временно недоступны."
                            )
                    except Exception:
                        logger.exception("Automatic chart failed client=%s", client.id)
                        await message.answer(
                            "Текстовый анализ готов, но график построить не удалось."
                        )
            finally:
                if chart_task and not chart_task.done():
                    chart_task.cancel()
                    await asyncio.gather(chart_task, return_exceptions=True)
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

    async def launch_chart(message, user_id, client_id, period_name):
        presentation = ReportMessage(message)

        async def work():
            state = {"stage": "загружаю дневную статистику Директа"}
            token = progress_state.set(state)
            try:
                await presentation.start(state)
                client = runtime.registry.require(message.chat.id, client_id)
                period = make_period(period_name)
                data = await runtime.checks.dynamics(client, period)
                if any(result.status.value not in ("ok", "no_data") for result in data):
                    await presentation.finish(
                        "График не построен: дневная статистика Директа временно недоступна."
                    )
                    return
                state["stage"] = "рисую график"
                await presentation.stop()
                if presentation.status:
                    try:
                        await presentation.status.delete()
                    except TelegramBadRequest:
                        pass
                await send_chart(message, client, period, data)
                await runtime.checks.repository.save_conversation(
                    message.chat.id,
                    user_id,
                    None,
                    active_client_id=client.id,
                    period=period.model_dump(mode="json"),
                )
            finally:
                await presentation.stop()
                progress_state.reset(token)

        async def failed():
            await presentation.finish(
                "Не удалось построить график. Ошибка записана в журнал; попробуйте позже."
            )

        if not runtime.jobs.start((message.chat.id, user_id), work, failed):
            await message.answer("Другой запрос уже выполняется или все рабочие слоты заняты.")

    show_menu, clear_menu = install_menu(router, runtime, launch, launch_chart)

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
            await answer_callback(
                callback, "Выбор устарел или предназначен другому пользователю.", show_alert=True
            )
            return
        pending.pop(nonce)
        await answer_callback(callback)
        try:
            async with asyncio.timeout(3):
                await callback.message.edit_reply_markup(reply_markup=None)
        except (TelegramAPIError, TimeoutError) as exc:
            logger.warning("Could not clear client selection: %s", type(exc).__name__)
        if command.name == "chart":
            await launch_chart(callback.message, user_id, client.id, command.period)
        else:
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
            await runtime.agent.cancel(message.chat.id, message.from_user.id)
            await message.answer(
                "Выбор отменён, контекст диалога очищен. Уже запущенные проверки продолжатся."
            )
        elif name == "schedule":
            if message.from_user.id not in runtime.settings.telegram_admin_user_ids:
                await message.answer("Команда доступна пользователям из TELEGRAM_ADMIN_USER_IDS.")
                return
            await message.answer(await runtime.schedule.describe())
        elif name in ("check", "check_all", "summary", "summary_all", "chart"):
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
            elif name == "chart":
                await launch_chart(message, message.from_user.id, clients[0].id, cmd.period)
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

        presentation = ReportMessage(message)
        state = {"stage": "разбираю вопрос и проверяю данные"}

        async def work():
            token = progress_state.set(state)
            logger.info("Question started message_id=%s", message.message_id)
            try:
                await presentation.start(state)
                run_before = await runtime.checks.repository.latest_run(
                    message.chat.id, message.from_user.id
                )
                before = await runtime.checks.repository.conversation(
                    message.chat.id, message.from_user.id
                )
                answer = await runtime.agent.ask(
                    message.text, message.chat.id, message.from_user.id
                )
                await presentation.finish(answer, markdown=True)
                run_after = await runtime.checks.repository.latest_run(
                    message.chat.id, message.from_user.id
                )
                if run_after and (not run_before or run_after["id"] != run_before["id"]):
                    reports = [ClientReport.model_validate(value) for value in run_after["reports"]]
                    await offer_details(message, reports, message.from_user.id)
                mentioned = mentioned_clients(message.text, message.chat.id)
                if (
                    ANALYTICS_WORDS.search(message.text)
                    and not ALL_CLIENTS_WORDS.search(message.text)
                    and len(mentioned) <= 1
                ):
                    context = await runtime.checks.repository.conversation(
                        message.chat.id, message.from_user.id
                    )
                    context_changed = context.get("active_client_id") != before.get(
                        "active_client_id"
                    ) or context.get("period") != before.get("period")
                    if (
                        context.get("active_client_id")
                        and context.get("period")
                        and (mentioned or context_changed)
                    ):
                        try:
                            client = runtime.registry.require(
                                message.chat.id, context["active_client_id"]
                            )
                            period = AnalysisPeriod.model_validate(context["period"])
                            if not await send_chart(message, client, period):
                                await message.answer(
                                    "Текстовый анализ готов, но Директ не вернул дневные "
                                    "данные для графика за весь выбранный период."
                                )
                        except Exception:
                            logger.exception("Question chart failed")
                            await message.answer(
                                "Текстовый анализ готов, но график не удалось сформировать "
                                "или отправить. Ошибка записана в журнал."
                            )
            except Exception:
                logger.exception("Question failed stage=%s", state["stage"])
                raise
            finally:
                await presentation.stop()
                progress_state.reset(token)

        async def failed():
            await presentation.finish("Не удалось обработать вопрос. Попробуйте /check <клиент>.")

        if not runtime.jobs.start((message.chat.id, message.from_user.id), work, failed):
            await message.answer("Ваш запрос уже выполняется или все рабочие слоты заняты.")

    dp.include_router(router)
    return dp
