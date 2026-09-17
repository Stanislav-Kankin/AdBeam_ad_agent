"""Owner-bound inline navigation over clients authorized for the current chat."""

import secrets
from time import monotonic

from aiogram import F
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.domain.reports import CheckMode

PAGE_SIZE = 8
GOAL_PAGE_SIZE = 7
PERIODS = (
    ("Вчера", "yesterday"),
    ("7 дней", "7d"),
    ("14 дней", "14d"),
    ("30 дней", "30d"),
    ("90 дней", "90d"),
)


def install_menu(router, runtime, launch):
    actions = {}

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
                row("🕙 Расписание", "schedule")
                if runtime.discovery:
                    row("🔄 Обновить клиентов Яндекса", "refresh")
            row("❓ Помощь", "help")
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
                if client_id and runtime.inventory and user in runtime.settings.telegram_admin_user_ids:
                    row("⚙️ Данные и цели", "data", client_id=client_id, page=page)
            else:
                text += "\nВыберите период завершённых дней (МСК)."
                for label, period in PERIODS:
                    row(label, "run", client_id=client_id, mode=mode, period=period)
                row("← Тип отчёта", "report", client_id=client_id, page=page)
            row("← Клиенты", "clients", page=page)
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
                marker = "✅" if counter["selected"] else "⛔" if counter["status"] != "ok" else "🔗" if counter["linked"] else "▫️"
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
            ordered = sorted(goals.values(), key=lambda value: value["name"].casefold())
            pages = max(1, (len(ordered) + GOAL_PAGE_SIZE - 1) // GOAL_PAGE_SIZE)
            page = min(max(page, 0), pages - 1)
            selected = set(client.metrica.main_goal_ids)
            text = badge + (
                f"Основные цели\n{client.name}\n"
                f"Выбрано: {len(selected)} из 10 · Страница {page + 1} из {pages}"
            )
            if not ordered:
                text += "\nКаталог целей ещё не загружен. Обновите доступы."
            for goal in ordered[page * GOAL_PAGE_SIZE : (page + 1) * GOAL_PAGE_SIZE]:
                row(
                    ("✅ " if goal["id"] in selected else "▫️ ") + goal["name"][:90],
                    "toggle_goal",
                    client_id=client_id,
                    goal_id=goal["id"],
                    page=page,
                )
            navigation = []
            if page:
                navigation.append(button("← Назад", "goals", client_id=client_id, page=page - 1))
            if page + 1 < pages:
                navigation.append(
                    button("Вперёд →", "goals", client_id=client_id, page=page + 1)
                )
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
            await message.edit_text(text, reply_markup=markup, parse_mode=None)
        else:
            await message.answer(text, reply_markup=markup, parse_mode=None)

    @router.callback_query(F.data.startswith("menu:"))
    async def navigate(callback):
        item = actions.get(callback.data.removeprefix("menu:"))
        if (
            not item
            or monotonic() - item[0] > 600
            or not callback.message
            or item[1:3] != (callback.message.chat.id, callback.from_user.id)
        ):
            await callback.answer(
                "Меню устарело или принадлежит другому пользователю. Откройте /menu.",
                show_alert=True,
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
            await callback.answer("Доступ больше не разрешён. Откройте /menu.", show_alert=True)
            return
        clear(chat, user)
        await callback.answer()
        if action == "refresh":
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
                await launch(callback.message, user, ids, kwargs["period"], kwargs["mode"])
            else:
                await callback.message.answer("Нет доступных активных клиентов.")
        else:
            await show(callback.message, user, screen=action, edit=True, **kwargs)

    return show, clear
