"""Owner-bound inline navigation over clients authorized for the current chat."""

import secrets
from time import monotonic

from aiogram import F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.callbacks import answer_callback
from app.bot.report_message import retry_telegram
from app.domain.reports import CheckMode
from app.reporting.data_status import describe_data

PAGE_SIZE = 8
GOAL_PAGE_SIZE = 7
PERIODS = (
    ("Вчера", "yesterday"),
    ("7 дней", "7d"),
    ("14 дней", "14d"),
    ("30 дней", "30d"),
    ("90 дней", "90d"),
)


def install_menu(router, runtime, launch, launch_chart):
    actions = {}
    awaiting_user = {}

    @router.message(
        lambda message: (
            message.text is not None and (message.chat.id, message.from_user.id) in awaiting_user
        )
    )
    async def add_user_input(message):
        key = (message.chat.id, message.from_user.id)
        started = awaiting_user.pop(key)
        if message.from_user.id not in runtime.settings.telegram_admin_user_ids:
            return
        if message.text.strip() in ("/cancel", "/menu", "/start"):
            await show(message, message.from_user.id)
            return
        value = message.text.strip()
        if monotonic() - started > 600:
            await message.answer("Время ввода истекло. Откройте раздел «Пользователи» заново.")
            return
        if not value.isascii() or not value.isdigit() or not 0 < int(value) < 2**63:
            awaiting_user[key] = started
            await message.answer("Нужен числовой Telegram ID пользователя. /cancel — отмена.")
            return
        uid = int(value)
        if uid in runtime.settings.telegram_admin_user_ids:
            await message.answer("Это администратор из .env. Его права уже настроены.")
        else:
            ids = [c.id for c in runtime.registry.visible(message.chat.id)]
            await runtime.checks.repository.set_bot_user(uid, True, ids, message.from_user.id)
            await message.answer(
                f"Пользователь {uid} добавлен. Доступных клиентов: {len(ids)}. "
                "Теперь он может открыть личный чат с ботом и отправить /start."
            )
        await show(message, message.from_user.id, screen="users")

    def clear(chat, user):
        for key, value in list(actions.items()):
            if value[1:3] == (chat, user) or monotonic() - value[0] > 600:
                actions.pop(key, None)

    async def show(message, user, screen="home", page=0, client_id=None, mode=None, edit=False):
        chat = message.chat.id
        clear(chat, user)
        clients = runtime.registry.visible(chat)
        rows = []

        def button(label, action, **kwargs):
            token = secrets.token_hex(8)
            actions[token] = (monotonic(), chat, user, action, kwargs)
            return InlineKeyboardButton(text=label, callback_data="menu:" + token)

        def row(label, action, **kwargs):
            rows.append([button(label, action, **kwargs)])

        badge = "🧪 MOCK — тестовые данные\n" if runtime.settings.app_mode == "mock" else ""
        if screen == "home":
            text = badge + "AdBeam — аналитика\nВыберите действие."
            row("👥 Клиенты", "clients")
            row("📊 Аналитика всех клиентов", "report")
            if user in runtime.settings.telegram_admin_user_ids:
                row("👤 Пользователи", "users")
                row("🕙 Расписание", "schedule")
                if runtime.discovery:
                    row("🔄 Обновить клиентов Яндекса", "refresh")
            row("❓ Помощь", "help")
        elif screen == "users":
            if user not in runtime.settings.telegram_admin_user_ids:
                raise PermissionError
            members = await runtime.checks.repository.bot_users()
            configured = set(runtime.settings.telegram_allowed_user_ids) | {
                uid for uid in runtime.settings.telegram_allowed_chat_ids if uid > 0
            }
            ids = sorted(
                configured | members.keys() | set(runtime.settings.telegram_admin_user_ids)
            )
            pages = max(1, (len(ids) + PAGE_SIZE - 1) // PAGE_SIZE)
            page = min(max(page, 0), pages - 1)
            text = badge + f"Пользователи · страница {page + 1}/{pages}\n"
            text += (
                "Добавление открывает личный чат и доступ к вашим текущим клиентам. "
                "Новые клиенты позже автоматически не добавляются.\n"
                "Удаление отзывает доступ к боту, в том числе в разрешённых группах. "
                "Уже запущенная работа может завершиться.\n"
            )
            for uid in ids[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
                if uid in runtime.settings.telegram_admin_user_ids:
                    text += f"\n🔐 {uid} — администратор (.env)"
                else:
                    enabled = members.get(uid, {}).get("enabled", True)
                    text += f"\n{'✅' if enabled else '⛔'} {uid}"
                    row(
                        ("Удалить " if enabled else "Добавить снова ") + str(uid),
                        "member_revoke" if enabled else "member_restore",
                        member_id=uid,
                        page=page,
                    )
            if not runtime.settings.telegram_allowed_user_ids:
                text += "\n\nВ группах из .env действует прежний доступ для участников; список выше не является списком всех участников групп."
            row("➕ Добавить по Telegram ID", "member_add")
            if page:
                row("← Назад", "users", page=page - 1)
            if page + 1 < pages:
                row("Вперёд →", "users", page=page + 1)
        elif screen == "clients":
            pages = max(1, (len(clients) + PAGE_SIZE - 1) // PAGE_SIZE)
            page = min(max(page, 0), pages - 1)
            text = badge + f"Подключённые клиенты: {len(clients)}\nСтраница {page + 1} из {pages}"
            if not clients:
                text += "\nНет доступных активных клиентов."
            for client in clients[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
                row(client.name[:100], "report", client_id=client.id, page=page)
            navigation = []
            if page:
                navigation.append(button("← Назад", "clients", page=page - 1))
            if page + 1 < pages:
                navigation.append(button("Вперёд →", "clients", page=page + 1))
            if navigation:
                rows.append(navigation)
        elif screen in ("report", "period"):
            title = (
                runtime.registry.require(chat, client_id).name
                if client_id
                else "Все доступные клиенты"
            )
            text = badge + title
            if not clients:
                text += "\nНет доступных активных клиентов."
            elif screen == "report":
                text += "\nКакой отчёт подготовить?"
                row(
                    "🔎 Подробная проверка",
                    "period",
                    client_id=client_id,
                    page=page,
                    mode=CheckMode.STANDARD,
                )
                row(
                    "📋 Краткая сводка",
                    "period",
                    client_id=client_id,
                    page=page,
                    mode=CheckMode.SUMMARY,
                )
                if client_id:
                    row("🗂 Состояние данных", "status", client_id=client_id, page=page)
                    row(
                        "📈 График динамики", "period", client_id=client_id, page=page, mode="chart"
                    )
                if (
                    client_id
                    and runtime.inventory
                    and user in runtime.settings.telegram_admin_user_ids
                ):
                    row("⚙️ Данные и цели", "data", client_id=client_id, page=page)
            else:
                text += "\nВыберите период завершённых дней (МСК)."
                for label, period in PERIODS:
                    row(label, "run", client_id=client_id, mode=mode, period=period)
                row("← Тип отчёта", "report", client_id=client_id, page=page)
            row("← Клиенты", "clients", page=page)
        elif screen == "status":
            text = badge + await describe_data(runtime, chat, client_id)
            row("🔄 Обновить экран", "status", client_id=client_id, page=page)
            row("🔎 Проверить клиента", "period", client_id=client_id, mode=CheckMode.STANDARD)
            if runtime.inventory and user in runtime.settings.telegram_admin_user_ids:
                row("⚙️ Данные и цели", "data", client_id=client_id)
            row("← К отчёту", "report", client_id=client_id, page=page)
        elif screen == "data":
            if user not in runtime.settings.telegram_admin_user_ids or not runtime.inventory:
                raise PermissionError
            client = runtime.registry.require(chat, client_id)
            counters = await runtime.checks.repository.client_counters(client_id)
            selected = [counter for counter in counters if counter["selected"]]
            text = badge + f"Настройка данных\n{client.name}\n"
            if not counters:
                text += "\nДоступы ещё не проверены. Нажмите «Обновить доступы»."
            else:
                available = sum(counter["status"] == "ok" for counter in counters)
                forbidden = sum(counter["status"] != "ok" for counter in counters)
                text += (
                    f"\nСвязанных счётчиков: {len(counters)}"
                    f"\nДоступно: {available}; без доступа: {forbidden}"
                    f"\nВыбрано: {len(selected)}"
                    f"\nОсновных целей: {len(client.metrica.main_goal_ids)} из 10"
                )
            row("🔄 Обновить доступы", "refresh_data", client_id=client_id, page=page)
            row("📟 Выбрать счётчики", "counters", client_id=client_id, page=0)
            if selected:
                row("🎯 Выбрать основные цели", "goals", client_id=client_id, page=0)
            row("← К отчёту", "report", client_id=client_id, page=page)
        elif screen == "counters":
            if user not in runtime.settings.telegram_admin_user_ids or not runtime.inventory:
                raise PermissionError
            include_all = bool(mode == "all")
            counters = await runtime.checks.repository.client_counters(
                client_id, include_all=include_all
            )
            pages = max(1, (len(counters) + PAGE_SIZE - 1) // PAGE_SIZE)
            page = min(max(page, 0), pages - 1)
            text = badge + (
                "Все доступные счётчики Метрики"
                if include_all
                else "Счётчики, связанные с кампаниями"
            )
            text += f"\nСтраница {page + 1} из {pages}\n✅ выбран · 🔗 связан · ⛔ нет доступа"
            for counter in counters[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
                marker = (
                    "✅"
                    if counter["selected"]
                    else "⛔"
                    if counter["status"] != "ok"
                    else "🔗"
                    if counter["linked"]
                    else "▫️"
                )
                label = f"{marker} {counter['name'] or counter['site'] or counter['id']} · {counter['id']}"
                row(
                    label[:100],
                    "toggle_counter",
                    client_id=client_id,
                    counter_id=counter["id"],
                    page=page,
                    mode="all" if include_all else None,
                )
            navigation = []
            if page:
                navigation.append(
                    button("← Назад", "counters", client_id=client_id, page=page - 1, mode=mode)
                )
            if page + 1 < pages:
                navigation.append(
                    button("Вперёд →", "counters", client_id=client_id, page=page + 1, mode=mode)
                )
            if navigation:
                rows.append(navigation)
            if include_all:
                row("🔗 Только связанные", "counters", client_id=client_id, page=0)
            else:
                row("📚 Все доступные", "counters", client_id=client_id, page=0, mode="all")
            row("← Настройка данных", "data", client_id=client_id)
        elif screen == "goals":
            if user not in runtime.settings.telegram_admin_user_ids or not runtime.inventory:
                raise PermissionError
            client = runtime.registry.require(chat, client_id)
            counters = await runtime.checks.repository.client_counters(client_id)
            goals = {
                goal["id"]: goal
                for counter in counters
                if counter["selected"] and counter["status"] == "ok"
                for goal in counter["goals"]
            }
            selected = set(client.metrica.main_goal_ids)
            business_words = (
                "конвер",
                "заказ",
                "заяв",
                "покуп",
                "звон",
                "расч",
                "цен",
                "оплат",
            )
            ordered = sorted(
                goals.values(),
                key=lambda value: (
                    value["id"] not in selected,
                    not any(word in value["name"].casefold() for word in business_words),
                    value["name"].casefold(),
                ),
            )
            pages = max(1, (len(ordered) + GOAL_PAGE_SIZE - 1) // GOAL_PAGE_SIZE)
            page = min(max(page, 0), pages - 1)
            text = badge + (
                f"Основные цели\n{client.name}\n"
                f"Выбрано: {len(selected)} из 10 · Страница {page + 1} из {pages}\n"
                "Не выбирайте пересекающиеся цели: их достижения суммируются."
            )
            if not ordered:
                text += "\nКаталог целей ещё не загружен. Обновите доступы."
            for goal in ordered[page * GOAL_PAGE_SIZE : (page + 1) * GOAL_PAGE_SIZE]:
                row(
                    (
                        ("✅ " if goal["id"] in selected else "▫️ ")
                        + goal["name"]
                        + f" · {goal['id']}"
                    )[:100],
                    "toggle_goal",
                    client_id=client_id,
                    goal_id=goal["id"],
                    page=page,
                )
            navigation = []
            if page:
                navigation.append(button("← Назад", "goals", client_id=client_id, page=page - 1))
            if page + 1 < pages:
                navigation.append(button("Вперёд →", "goals", client_id=client_id, page=page + 1))
            if navigation:
                rows.append(navigation)
            row("← Настройка данных", "data", client_id=client_id)
        elif screen == "schedule":
            if user not in runtime.settings.telegram_admin_user_ids:
                raise PermissionError
            text = await runtime.schedule.describe()
        else:
            from app.bot.commands import HELP

            text = HELP
        if screen != "home":
            row("🏠 Главное меню", "home")
        # Bound memory even when many authorized users open menus.
        while len(actions) > 4000:
            actions.pop(next(iter(actions)))
        markup = InlineKeyboardMarkup(inline_keyboard=rows)
        if edit:
            try:
                await retry_telegram(
                    lambda: message.edit_text(
                        text, reply_markup=markup, parse_mode=None, request_timeout=7
                    )
                )
            except TelegramBadRequest as exc:
                if "message is not modified" not in exc.message:
                    raise
        else:
            await retry_telegram(
                lambda: message.answer(
                    text, reply_markup=markup, parse_mode=None, request_timeout=7
                )
            )

    @router.callback_query(F.data.startswith("menu:"))
    async def navigate(callback):
        item = actions.get(callback.data.removeprefix("menu:"))
        if (
            not item
            or monotonic() - item[0] > 600
            or not callback.message
            or item[1:3] != (callback.message.chat.id, callback.from_user.id)
        ):
            answered = await answer_callback(
                callback,
                "Меню устарело или принадлежит другому пользователю. Откройте /menu.",
                show_alert=True,
            )
            if not answered and callback.message:
                await retry_telegram(
                    lambda: callback.message.answer(
                        "Эта кнопка больше не действует. Откройте новое меню: /menu.",
                        parse_mode=None,
                        request_timeout=7,
                    )
                )
            return
        _, chat, user, action, kwargs = item
        try:
            client_id = kwargs.get("client_id")
            if client_id:
                runtime.registry.require(chat, client_id)
            if (
                action
                in (
                    "schedule",
                    "users",
                    "member_add",
                    "member_revoke",
                    "member_restore",
                    "refresh",
                    "data",
                    "refresh_data",
                    "counters",
                    "toggle_counter",
                    "goals",
                    "toggle_goal",
                )
                and user not in runtime.settings.telegram_admin_user_ids
            ):
                raise PermissionError
        except (PermissionError, KeyError):
            await answer_callback(
                callback, "Доступ больше не разрешён. Откройте /menu.", show_alert=True
            )
            return
        clear(chat, user)
        await answer_callback(callback)
        if action == "member_add":
            awaiting_user[(chat, user)] = monotonic()
            await callback.message.answer(
                "Пришлите числовой Telegram ID пользователя. "
                "Он получит доступ в личном чате ко всем клиентам, видимым вам здесь. /cancel — отмена."
            )
        elif action in ("member_revoke", "member_restore"):
            uid = kwargs["member_id"]
            if uid in runtime.settings.telegram_admin_user_ids:
                await callback.message.answer("Администраторы управляются через .env.")
                return
            await runtime.checks.repository.set_bot_user(
                uid,
                action == "member_restore",
                [c.id for c in runtime.registry.visible(chat)]
                if action == "member_restore"
                else [],
                user,
            )
            await show(
                callback.message, user, screen="users", page=kwargs.get("page", 0), edit=True
            )
        elif action == "refresh":
            await show(callback.message, user, edit=True)

            async def work():
                await callback.message.answer("Загружаю клиентов из аккаунта Яндекса.")
                count = await runtime.discovery.refresh()
                await callback.message.answer(f"Список обновлён. Клиентов: {count}.")
                await show(callback.message, user, screen="clients")

            async def failed():
                await callback.message.answer(
                    "Не удалось получить клиентов Яндекса. Проверьте токен и доступ к API."
                )

            if not runtime.jobs.start((chat, user), work, failed):
                await callback.message.answer("Запрос уже выполняется. Попробуйте позже.")
        elif action == "refresh_data":
            await show(callback.message, user, screen="data", edit=True, **kwargs)

            async def work():
                client = runtime.registry.require(chat, kwargs["client_id"])
                await callback.message.answer("Проверяю связанные счётчики и доступ Метрики.")
                counters = await runtime.inventory.refresh_client(client, refresh=True)
                for counter in counters:
                    if counter["selected"] and counter["status"] == "ok":
                        await runtime.inventory.goals(client, counter["id"])
                await callback.message.answer("Доступы и каталог целей обновлены.")
                await show(callback.message, user, screen="data", **kwargs)

            async def failed():
                await callback.message.answer(
                    "Не удалось обновить доступы. Проверьте токены Директа и Метрики."
                )

            if not runtime.jobs.start((chat, user), work, failed):
                await callback.message.answer("Запрос уже выполняется. Попробуйте позже.")
        elif action == "toggle_counter":
            client = runtime.registry.require(chat, kwargs["client_id"])
            counters = await runtime.checks.repository.client_counters(client.id, include_all=True)
            selected = {counter["id"] for counter in counters if counter["selected"]}
            counter_id = int(kwargs["counter_id"])
            if counter_id in selected:
                selected.remove(counter_id)
            else:
                selected.add(counter_id)
            client = await runtime.inventory.select_counters(client, sorted(selected), user)
            if counter_id in selected:
                await runtime.inventory.goals(client, counter_id)
            await show(
                callback.message,
                user,
                screen="counters",
                edit=True,
                client_id=client.id,
                page=kwargs.get("page", 0),
                mode=kwargs.get("mode"),
            )
        elif action == "toggle_goal":
            client = runtime.registry.require(chat, kwargs["client_id"])
            selected = set(client.metrica.main_goal_ids)
            goal_id = str(kwargs["goal_id"])
            if goal_id in selected:
                selected.remove(goal_id)
            elif len(selected) >= 10:
                await callback.message.answer("Можно выбрать не более 10 основных целей.")
            else:
                selected.add(goal_id)
            await runtime.inventory.select_goals(client, sorted(selected), user)
            await show(
                callback.message,
                user,
                screen="goals",
                edit=True,
                client_id=client.id,
                page=kwargs.get("page", 0),
            )
        elif action == "run":
            ids = [client_id] if client_id else [c.id for c in runtime.registry.visible(chat)]
            await show(callback.message, user, edit=True)
            if ids:
                if kwargs["mode"] == "chart":
                    await launch_chart(callback.message, user, ids[0], kwargs["period"])
                else:
                    await launch(callback.message, user, ids, kwargs["period"], kwargs["mode"])
            else:
                await callback.message.answer("Нет доступных активных клиентов.")
        else:
            await show(callback.message, user, screen=action, edit=True, **kwargs)

    return show, clear
