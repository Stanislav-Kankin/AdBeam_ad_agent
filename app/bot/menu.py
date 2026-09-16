"""Owner-bound inline navigation over clients authorized for the current chat."""

import secrets
from time import monotonic

from aiogram import F
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.domain.reports import CheckMode

PAGE_SIZE = 8
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
            else:
                text += "\nВыберите период завершённых дней (МСК)."
                for label, period in PERIODS:
                    row(label, "run", client_id=client_id, mode=mode, period=period)
                row("← Тип отчёта", "report", client_id=client_id, page=page)
            row("← Клиенты", "clients", page=page)
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
                action in ("schedule", "refresh")
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
