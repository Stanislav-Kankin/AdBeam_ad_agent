import asyncio
import logging
import re
import secrets
from datetime import UTC, datetime, time
from time import monotonic

from aiogram import Dispatcher, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)

from app.analytics.mediaplan import plan_fact_text
from app.analytics.periods import MOSCOW, AnalysisPeriod, make_period, today_moscow
from app.analytics.progress import progress_state
from app.analytics.trend import load_trend, summary
from app.bot.callbacks import answer_callback
from app.bot.commands import HELP, parse_command
from app.bot.markdown import markdown_parts
from app.bot.menu import install_menu
from app.bot.middleware import AccessMiddleware
from app.bot.report_message import ReportMessage, report_entities, retry_telegram
from app.domain.reports import CheckMode, ClientReport, TriggerSource
from app.reporting.balance import balance_text, feedback_text
from app.reporting.charts import render_audience, render_dynamics, render_trend
from app.reporting.formatter import (
    audience_report,
    audience_segments,
    campaigns_view,
    detailed,
    risk_options,
    split_message,
    weekly_digest,
)
from app.scheduler.jobs import DailySchedule

logger = logging.getLogger(__name__)

ANALYTICS_WORDS = re.compile(
    r"анализ|аналитик|обзор|отч[её]т|динамик|показател|расход|клик|конверс|"
    r"трафик|кампан|директ|метрик|\bcpa\b|\bctr\b|\bcpc\b|\bcr\b|дрр",
    re.IGNORECASE,
)
ALL_CLIENTS_WORDS = re.compile(r"все\s+клиент|по\s+всем\s+клиент|всех\s+клиент", re.IGNORECASE)


def topic_of(message):
    """Forum topic of a group message: replies must stay in it, not in General."""
    return message.message_thread_id if message and message.is_topic_message else None


async def send_text(bot, chat_id, text, *, markdown=False, thread_id=None):
    if markdown:
        for part, entities in markdown_parts(text):
            await send_part(bot, chat_id, part, entities=entities, thread_id=thread_id)
        return
    for part in split_message(text):
        await send_part(bot, chat_id, part, thread_id=thread_id)


async def send_part(bot, chat_id, text, entities=None, thread_id=None):
    return await retry_telegram(
        lambda: bot.send_message(
            chat_id,
            text,
            parse_mode=None,
            entities=report_entities(text) if entities is None else entities,
            message_thread_id=thread_id,
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

    # 👍/👎 buttons live a day: long enough to rate a report read later.
    rated = {}
    awaiting_comment = {}

    def feedback_row(chat_id, user_id, kind, client_id, excerpt):
        now = monotonic()
        for key in [k for k, v in rated.items() if now - v[0] > 86400]:
            rated.pop(key, None)
        while len(rated) >= 500:
            rated.pop(next(iter(rated)))
        token = secrets.token_hex(6)
        rated[token] = (now, chat_id, user_id, kind, client_id, excerpt[:1000])
        return [
            InlineKeyboardButton(text="👍 Полезно", callback_data=f"fb:{token}:1"),
            InlineKeyboardButton(text="👎 Не то", callback_data=f"fb:{token}:-1"),
        ]

    @router.callback_query(F.data.startswith("fb:"))
    async def rate(callback):
        try:
            _, token, value = callback.data.split(":", 2)
            created, chat_id, user_id, kind, client_id, excerpt = rated.pop(token)
            if chat_id != callback.message.chat.id or user_id != callback.from_user.id:
                rated[token] = (created, chat_id, user_id, kind, client_id, excerpt)
                raise ValueError
        except (KeyError, ValueError):
            await answer_callback(callback, "Оценка уже учтена или кнопка устарела.")
            return
        feedback_id = await runtime.checks.repository.save_feedback(
            chat_id, user_id, kind, client_id, int(value), excerpt
        )
        if value == "1":
            await answer_callback(callback, "Спасибо! Отметили как полезное.")
            return
        await answer_callback(callback)
        awaiting_comment[(chat_id, user_id)] = (monotonic(), feedback_id)
        await callback.message.answer(
            "Что было не так? Напишите одним сообщением, это прочитает разработчик. "
            "/cancel — пропустить.",
            parse_mode=None,
        )

    @router.message(
        lambda message: (
            message.text is not None and (message.chat.id, message.from_user.id) in awaiting_comment
        )
    )
    async def feedback_comment(message):
        started, feedback_id = awaiting_comment.pop((message.chat.id, message.from_user.id))
        text = message.text.strip()
        if text.split("@", 1)[0] in ("/cancel", "/menu", "/start") or monotonic() - started > 1800:
            # Not a comment: let the command or question run as usual.
            raise SkipHandler
        await runtime.checks.repository.comment_feedback(feedback_id, text)
        await message.answer("Спасибо, записал. Разберём и поправим.", parse_mode=None)

    @router.callback_query(F.data == "ctx:switch")
    async def switch_client(callback):
        await answer_callback(callback)
        await runtime.agent.cancel(callback.message.chat.id, callback.from_user.id)
        await show_menu(callback.message, callback.from_user.id, screen="clients")

    def can_configure(user):
        return user in runtime.settings.telegram_admin_user_ids or user in getattr(
            runtime.registry, "user_grants", {}
        )

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
            campaigns_view(reports[0], limit=5),
            detailed(reports[0]),
            reports[0].client_id,
            reports[0].period.model_dump(mode="json"),
            risk_options(reports[0]) if can_configure(user_id) else [],
        )
        snooze = (
            [[InlineKeyboardButton(text="🔕 Скрыть риск", callback_data=f"details:{token}:snooze")]]
            if report_details[token][7]
            else []
        )
        try:
            await retry_telegram(
                lambda: message.answer(
                    "Дополнительные данные",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="📊 Тренд за полгода",
                                    callback_data=f"details:{token}:trend",
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
                                    text="📋 Кампании: главные изменения",
                                    callback_data=f"details:{token}:specialist",
                                )
                            ],
                            *snooze,
                            feedback_row(
                                message.chat.id,
                                user_id,
                                "report",
                                reports[0].client_id,
                                f"Отчёт · {reports[0].client_name} · "
                                f"{reports[0].period.current.label()}",
                            ),
                        ]
                        # Raw technical detail is for admins; managers do not need it.
                        + (
                            [
                                [
                                    InlineKeyboardButton(
                                        text="⚙️ Технические данные",
                                        callback_data=f"details:{token}:technical",
                                    )
                                ]
                            ]
                            if user_id in runtime.settings.telegram_admin_user_ids
                            else []
                        )
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
            (
                created,
                chat_id,
                user_id,
                specialist,
                technical,
                client_id,
                period_data,
                options,
            ) = report_details[token]
            hide = re.fullmatch(r"z(\d)", view)
            if (
                monotonic() - created > 900
                or chat_id != callback.message.chat.id
                or user_id != callback.from_user.id
                or (
                    view not in ("specialist", "technical", "audience", "trend", "snooze")
                    and not (hide and int(hide[1]) < len(options))
                )
            ):
                raise ValueError
        except (KeyError, ValueError):
            await answer_callback(
                callback, "Расшифровка устарела. Запустите проверку ещё раз.", show_alert=True
            )
            return
        await answer_callback(callback)
        if view == "snooze":
            await retry_telegram(
                lambda: callback.message.answer(
                    "Какой риск скрыть на 7 дней? Он перестанет подсвечиваться в отчётах и "
                    "сводках этого клиента, цифры кампании останутся. Вернуть можно в "
                    "«KPI проекта».",
                    reply_markup=InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text=f"🔕 {label}"[:60],
                                    callback_data=f"details:{token}:z{index}",
                                )
                            ]
                            for index, (_, label) in enumerate(options)
                        ]
                    ),
                    request_timeout=15,
                )
            )
            return
        if hide:
            key, label = options[int(hide[1])]
            if not can_configure(callback.from_user.id):
                return
            until = await runtime.checks.repository.snooze_risk(
                client_id, key, label, 7, callback.from_user.id
            )
            await callback.message.answer(
                f"Скрыто до {until.astimezone(MOSCOW):%d.%m}: «{label}». "
                "Вернуть раньше — «KPI проекта» → «Скрытые риски».",
                parse_mode=None,
            )
            return
        if view == "trend":
            presentation = ReportMessage(callback.message)

            async def trend_work():
                state = {"stage": "собираю полгода по неделям из Директа и Метрики"}
                token = progress_state.set(state)
                try:
                    await presentation.start(state)
                    client = runtime.registry.require(chat_id, client_id)
                    trend = await load_trend(runtime.checks, client)
                    image = await asyncio.to_thread(render_trend, client_label(client), trend)
                    await presentation.finish(summary(trend), markdown=True)
                    await retry_telegram(
                        lambda: callback.message.bot.send_photo(
                            chat_id,
                            BufferedInputFile(image, filename="adbeam-trend.png"),
                            caption=f"📊 {client_label(client)} · тренд за полгода",
                            message_thread_id=topic_of(callback.message),
                            request_timeout=30,
                        )
                    )
                finally:
                    await presentation.stop()
                    progress_state.reset(token)

            async def trend_failed():
                await presentation.finish(
                    "Не удалось собрать тренд за полгода. Ошибка записана в журнал."
                )

            if not runtime.jobs.start((chat_id, user_id), trend_work, trend_failed):
                await callback.message.answer("Другой запрос уже выполняется. Попробуйте позже.")
            return
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
                    await presentation.finish(
                        audience_report(client_label(client), payload), markdown=True
                    )
                    segments = {
                        key: audience_segments(payload, key) for key in ("gender", "age", "income")
                    }
                    slides = await asyncio.to_thread(
                        render_audience,
                        client_label(client),
                        period.current.label(),
                        segments,
                        payload.get("quality"),
                    )
                    media = [
                        InputMediaPhoto(
                            media=BufferedInputFile(slide, filename=f"audience-{i}.png"),
                            caption=f"👥 {client_label(client)} · аудитория" if i == 0 else None,
                        )
                        for i, slide in enumerate(slides)
                    ]
                    await retry_telegram(
                        lambda: callback.message.bot.send_media_group(
                            chat_id,
                            media,
                            message_thread_id=topic_of(callback.message),
                            request_timeout=30,
                        )
                    )
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
            await send_text(
                callback.message.bot,
                chat_id,
                specialist,
                markdown=True,
                thread_id=topic_of(callback.message),
            )
        else:
            await send_text(
                callback.message.bot, chat_id, technical, thread_id=topic_of(callback.message)
            )

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
                message_thread_id=topic_of(message),
                request_timeout=30,
            )
        )
        return True

    async def send_plan_fact(message, client):
        """Media plan pace under a single-client report; a report never fails on it."""
        try:
            result = await runtime.mediaplans.progress(client)
            if result:
                await send_text(
                    message.bot,
                    message.chat.id,
                    plan_fact_text(result),
                    markdown=True,
                    thread_id=topic_of(message),
                )
        except Exception:
            logger.exception("Media plan pace failed client=%s", client.id)

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
                if len(client_ids) == 1:
                    await send_plan_fact(message, client)
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
            await clear_menu(message.chat.id, message.from_user.id)
            for key in [
                k for k, v in pending.items() if v[1:3] == (message.chat.id, message.from_user.id)
            ]:
                pending.pop(key)
            await runtime.agent.cancel(message.chat.id, message.from_user.id)
            await message.answer(
                "Выбор отменён, контекст диалога очищен. Уже запущенные проверки продолжатся."
            )
        elif name == "digest":
            # The weekly digest on demand: all clients of this chat (the team view in a
            # group, the person's own clients in a private chat).
            ids = [c.id for c in runtime.registry.visible(message.chat.id)]
            if not ids:
                await message.answer("В этом чате нет доступных клиентов.")
                return
            presentation = ReportMessage(message)

            async def digest_work():
                state = {"stage": f"собираю прошлую неделю по {len(ids)} клиентам"}
                token = progress_state.set(state)
                try:
                    await presentation.start(state)
                    period = DailySchedule.last_week()
                    reports, _ = await runtime.checks.run_check(
                        ids,
                        period,
                        CheckMode.STANDARD,
                        TriggerSource.INTERNAL,
                        chat_id=message.chat.id,
                        user_id=message.from_user.id,
                    )
                    failed = [cid for cid in ids if cid not in {r.client_id for r in reports}]
                    title = (
                        "Ваши клиенты за неделю"
                        if message.chat.type == "private"
                        and message.from_user.id not in runtime.settings.telegram_admin_user_ids
                        else "Сводка за неделю"
                    )
                    plans = await runtime.mediaplans.digest_lines(
                        [runtime.registry.require(message.chat.id, r.client_id) for r in reports]
                    )
                    await presentation.finish(
                        weekly_digest(reports, period, failed, title=title, plans=plans),
                        markdown=True,
                    )
                finally:
                    await presentation.stop()
                    progress_state.reset(token)

            async def digest_failed():
                await presentation.finish("Не удалось собрать сводку. Ошибка записана в журнал.")

            if not runtime.jobs.start(
                (message.chat.id, message.from_user.id), digest_work, digest_failed
            ):
                await message.answer("Другой запрос уже выполняется. Попробуйте позже.")
        elif name == "feedback":
            if message.from_user.id not in runtime.settings.telegram_admin_user_ids:
                await message.answer("Команда доступна пользователям из TELEGRAM_ADMIN_USER_IDS.")
                return
            await message.answer(await feedback_text(runtime), parse_mode=None)
        elif name == "balance":
            if message.from_user.id not in runtime.settings.telegram_admin_user_ids:
                await message.answer("Команда доступна пользователям из TELEGRAM_ADMIN_USER_IDS.")
                return
            await message.answer(await balance_text(runtime), parse_mode=None)
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

        user = message.from_user.id
        if user not in runtime.settings.telegram_admin_user_ids and runtime.agent.llm:
            midnight = datetime.combine(today_moscow(), time.min, MOSCOW).astimezone(UTC)
            limit = runtime.settings.questions_per_user_daily
            if not await runtime.checks.repository.reserve_question(user, limit, midnight):
                await message.answer(
                    f"На сегодня лимит вопросов исчерпан ({limit}). Отчёты и кнопки /menu "
                    "работают без ограничений, вопросы снова можно задавать завтра.",
                    parse_mode=None,
                )
                return

        presentation = ReportMessage(message)
        state = {"stage": "разбираю вопрос и проверяю данные"}

        async def answer_buttons(answer):
            context = await runtime.checks.repository.conversation(message.chat.id, user)
            rows = [feedback_row(message.chat.id, user, "answer", None, answer)]
            try:
                active = runtime.registry.require(message.chat.id, context["active_client_id"])
                rows[0] = feedback_row(message.chat.id, user, "answer", active.id, answer)
                rows.append(
                    [
                        InlineKeyboardButton(
                            text=f"🔄 Сменить клиента · сейчас {active.name}"[:60],
                            callback_data="ctx:switch",
                        )
                    ]
                )
            except (KeyError, PermissionError):
                pass
            return InlineKeyboardMarkup(inline_keyboard=rows)

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
                await presentation.finish(
                    answer, markdown=True, reply_markup=await answer_buttons(answer)
                )
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
                            period = AnalysisPeriod.model_validate(context["period"])
                        except ValueError:
                            # Context saved before the fix or a range without comparison:
                            # there is nothing to chart, and that is not an error.
                            logger.info("Question chart skipped: no comparison period")
                            period = None
                        try:
                            client = runtime.registry.require(
                                message.chat.id, context["active_client_id"]
                            )
                            if period and not await send_chart(message, client, period):
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
