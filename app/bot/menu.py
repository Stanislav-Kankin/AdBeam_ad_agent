"""Owner-bound inline navigation over clients authorized for the current chat."""

import secrets
from decimal import Decimal, InvalidOperation
from time import monotonic

from aiogram import F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.analytics.rules import main_kpi
from app.bot.callbacks import answer_callback
from app.bot.report_message import retry_telegram
from app.domain.reports import CheckMode
from app.reporting.data_status import describe_data
from app.reporting.formatter import fmt_short

PAGE_SIZE = 8
GOAL_PAGE_SIZE = 7
PERIODS = (
    ("Вчера", "yesterday"),
    ("7 дней", "7d"),
    ("14 дней", "14d"),
    ("30 дней", "30d"),
    ("90 дней", "90d"),
)


KPI_NAMES = {"cpa": "CPA", "drr": "ДРР", "conversions": "Конверсии"}
TOLERANCES = (3, 5, 10)
TARGET_FIELDS = {"target_cpa": "целевой CPA, ₽", "target_drr": "целевой ДРР, %"}


def parse_exclusions(value):
    """Words from "бренд, brand"; "0", "-" or "нет" clears the list."""
    text = value.strip()
    if text.casefold() in ("0", "-", "нет", "—"):
        return []
    words = [word.strip() for word in text.replace(";", ",").split(",")]
    words = [word for word in dict.fromkeys(words) if word]
    if not words or len(words) > 10 or any(not 2 <= len(word) <= 60 for word in words):
        raise ValueError
    return words


def parse_target(value):
    """Positive number from '2 500', '2500,5' or '0'/'-' to clear the target."""
    cleaned = value.strip().replace(" ", "").replace(" ", "").replace(",", ".")
    if cleaned in ("0", "-", "нет"):
        return None
    number = Decimal(cleaned)
    if not number.is_finite() or number <= 0:
        raise InvalidOperation
    return number


MENU_TTL = 6 * 3600


def install_menu(router, runtime, launch, launch_chart):
    def can_configure(user):
        """Admins and users added in the bot may set goals, counters and KPI."""
        return user in runtime.settings.telegram_admin_user_ids or user in getattr(
            runtime.registry, "user_grants", {}
        )

    def person(uid, members):
        stored = members.get(uid, {})
        seen = getattr(runtime.registry, "user_names", {}).get(uid, (None, None))
        username = stored.get("username") or seen[0]
        name = stored.get("full_name") or seen[1]
        label = " ".join(part for part in (name, f"@{username}" if username else None) if part)
        return f"{label} · {uid}" if label else f"{uid} (имя появится, когда напишет боту)"

    actions = {}
    awaiting_user = {}
    awaiting_target = {}

    async def save_targets(client, targets, user):
        updated = await runtime.checks.repository.save_client_preferences(
            client, targets=targets, user_id=user
        )
        runtime.registry.clients[client.id] = updated
        return updated

    @router.message(
        lambda message: (
            message.text is not None and (message.chat.id, message.from_user.id) in awaiting_target
        )
    )
    async def target_input(message):
        key = (message.chat.id, message.from_user.id)
        started, client_id, field = awaiting_target.pop(key)
        if not can_configure(message.from_user.id):
            return
        if message.text.strip() in ("/cancel", "/menu", "/start"):
            await show(message, message.from_user.id)
            return
        if monotonic() - started > 600:
            await message.answer("Время ввода истекло. Откройте «KPI проекта» заново.")
            return
        try:
            value = (
                parse_exclusions(message.text)
                if field == "excluded_campaigns"
                else parse_target(message.text)
            )
            client = runtime.registry.require(message.chat.id, client_id)
        except (InvalidOperation, ValueError):
            awaiting_target[key] = (started, client_id, field)
            await message.answer(
                "Нужны слова через запятую (до 10, от 2 символов), например: бренд, brand."
                if field == "excluded_campaigns"
                else "Нужно положительное число, например 2500. 0 — убрать цель."
            )
            return
        except PermissionError:
            await message.answer("Клиент больше недоступен этому чату.")
            return
        await save_targets(client, {field: value}, message.from_user.id)
        await show(message, message.from_user.id, screen="kpi", client_id=client_id)

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
            try:
                # Works once the person has opened the bot; otherwise the name is
                # filled in on their first message.
                chat = await message.bot.get_chat(uid)
                await runtime.checks.repository.remember_user_name(
                    uid, chat.username, chat.full_name
                )
            except Exception:
                pass
            await message.answer(
                f"Пользователь {uid} добавлен. Доступных клиентов: {len(ids)}. "
                "Теперь он может открыть личный чат с ботом и отправить /start. "
                "Ему доступны отчёты и настройка целей, счётчиков и KPI своих клиентов."
            )
        await show(message, message.from_user.id, screen="users")

    def clear(chat, user):
        """/cancel: drop every menu this user opened in the chat."""
        for key, value in list(actions.items()):
            if value[1:3] == (chat, user) or monotonic() - value[0] > MENU_TTL:
                actions.pop(key, None)

    def prune(screen=None):
        # A new screen must not kill other menus still open in a group; only the
        # pressed screen (one-shot, so a launch button cannot run twice) and expired ones.
        for key, value in list(actions.items()):
            if value[5] == screen or monotonic() - value[0] > MENU_TTL:
                actions.pop(key, None)

    async def show(message, user, screen="home", page=0, client_id=None, mode=None, edit=False):
        chat = message.chat.id
        prune()
        clients = runtime.registry.visible(chat)
        rows = []
        screen_id = secrets.token_hex(4)

        def button(label, action, **kwargs):
            token = secrets.token_hex(8)
            actions[token] = (monotonic(), chat, user, action, kwargs, screen_id)
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
                    text += f"\n🔐 {person(uid, members)} — администратор (.env)"
                else:
                    enabled = members.get(uid, {}).get("enabled", True)
                    count = len(members.get(uid, {}).get("client_ids") or [])
                    text += f"\n{'✅' if enabled else '⛔'} {person(uid, members)}" + (
                        f" · клиентов: {count}" if uid in members else ""
                    )
                    if enabled and uid in members:
                        row(
                            f"📁 Клиенты ({count}) · {person(uid, members)}"[:60],
                            "member_clients",
                            member_id=uid,
                        )
                    row(
                        (("Удалить " if enabled else "Добавить снова ") + person(uid, members))[
                            :60
                        ],
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
        elif screen == "member_clients":
            if user not in runtime.settings.telegram_admin_user_ids:
                raise PermissionError
            members = await runtime.checks.repository.bot_users()
            member = members.get(client_id)
            if member is None:
                raise PermissionError
            mine = set(member["client_ids"] or [])
            pages = max(1, (len(clients) + PAGE_SIZE - 1) // PAGE_SIZE)
            page = min(max(page, 0), pages - 1)
            text = badge + (
                f"Клиенты · {person(client_id, members)}\n"
                f"Отмечено: {len(mine)} из {len(clients)} · страница {page + 1} из {pages}\n"
                "Отмеченные клиенты видны этому человеку в личке и приходят ему "
                "в сводке по понедельникам."
            )
            for client in clients[page * PAGE_SIZE : (page + 1) * PAGE_SIZE]:
                row(
                    ("✅ " if client.id in mine else "▫️ ") + client.name[:90],
                    "member_toggle_client",
                    member_id=client_id,
                    target=client.id,
                    page=page,
                )
            navigation = []
            if page:
                navigation.append(
                    button("← Назад", "member_clients", member_id=client_id, page=page - 1)
                )
            if page + 1 < pages:
                navigation.append(
                    button("Вперёд →", "member_clients", member_id=client_id, page=page + 1)
                )
            if navigation:
                rows.append(navigation)
            row("← Пользователи", "users")
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
                if client_id and runtime.inventory and can_configure(user):
                    row("⚙️ Данные и цели", "data", client_id=client_id, page=page)
                if client_id and can_configure(user):
                    row("🎯 KPI проекта", "kpi", client_id=client_id, page=page)
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
            if runtime.inventory and can_configure(user):
                row("⚙️ Данные и цели", "data", client_id=client_id)
            row("← К отчёту", "report", client_id=client_id, page=page)
        elif screen == "data":
            if not can_configure(user) or not runtime.inventory:
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
                    + {
                        "favorites": " (избранные в Метрике)",
                        "campaigns": " (из настроек кампаний)",
                    }.get(client.direct.goals_source, " (выбраны вручную)")
                )
            row("🔄 Обновить доступы", "refresh_data", client_id=client_id, page=page)
            row("📟 Выбрать счётчики", "counters", client_id=client_id, page=0)
            if selected:
                row("🎯 Выбрать основные цели", "goals", client_id=client_id, page=0)
            row("← К отчёту", "report", client_id=client_id, page=page)
        elif screen == "counters":
            if not can_configure(user) or not runtime.inventory:
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
                    "⛔"
                    if counter["status"] != "ok"
                    else "✅"
                    if counter["selected"]
                    else "🔗"
                    if counter["linked"]
                    else "▫️"
                )
                label = f"{marker} {counter['name'] or counter['site'] or counter['id']} · {counter['id']}"
                row(
                    label[:100],
                    (
                        "toggle_counter"
                        if counter["status"] == "ok" or counter["selected"]
                        else "counter_unavailable"
                    ),
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
            if not can_configure(user) or not runtime.inventory:
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
            source = {
                "favorites": "избранные в Метрике (автоматически)",
                "campaigns": "из настроек кампаний (в Метрике нет избранных)",
            }.get(client.direct.goals_source, "выбраны вручную в боте")
            text = badge + (
                f"Основные цели\n{client.name}\n"
                f"Сейчас: {source}\n"
                f"Выбрано: {len(selected)} из 10 · Страница {page + 1} из {pages}\n"
                "⭐ — избранная в Метрике. Удобнее всего отметить 1–2 главные цели звёздочкой "
                "в Метрике: бот возьмёт их сам. Не выбирайте пересекающиеся цели "
                "(звонок и клик по номеру): их достижения суммируются."
            )
            if client.direct.goals_source == "manual":
                row(
                    "⭐ Брать избранные из Метрики автоматически", "auto_goals", client_id=client_id
                )
            if not ordered:
                text += "\nКаталог целей ещё не загружен. Обновите доступы."
            for goal in ordered[page * GOAL_PAGE_SIZE : (page + 1) * GOAL_PAGE_SIZE]:
                row(
                    (
                        ("✅ " if goal["id"] in selected else "▫️ ")
                        + ("⭐ " if goal.get("favorite") else "")
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
        elif screen == "kpi":
            if not can_configure(user):
                raise PermissionError
            client = runtime.registry.require(chat, client_id)
            targets = client.targets
            kpi = main_kpi(client)
            tolerance = targets.kpi_change_tolerance_percent
            text = badge + (
                f"KPI проекта\n{client.name}\n\n"
                f"Главный KPI: {KPI_NAMES.get(kpi, 'не определён')}"
                f"{'' if targets.kpi else ' (автоматически)'}\n"
                f"Целевой CPA: "
                f"{fmt_short(targets.target_cpa, money=True) + ' ₽' if targets.target_cpa else 'не задан'}\n"
                f"Целевой ДРР: "
                f"{fmt_short(targets.target_drr) + '%' if targets.target_drr else 'не задан'}\n"
                f"Допуск (статпогрешность): {fmt_short(tolerance)}%\n"
                f"Не подсвечивать в рисках кампании со словами: "
                f"{', '.join(targets.excluded_campaigns) or 'нет'}\n\n"
                "Статус клиента считается по главному KPI. Если он в пределах допуска "
                "или улучшился, изменения расхода, CPC и CR показываются как контекст, "
                "а не как проблема."
            )
            if kpi is None:
                text += (
                    "\n\nБез основных целей CPA не рассчитывается: выберите цели в «Данные и цели»."
                )
            rows.append(
                [
                    button(
                        ("✅ " if targets.kpi == value else "") + name,
                        "set_kpi",
                        client_id=client_id,
                        value=value,
                    )
                    for value, name in KPI_NAMES.items()
                ]
                + [
                    button(
                        ("✅ " if targets.kpi is None else "") + "Авто",
                        "set_kpi",
                        client_id=client_id,
                        value=None,
                    )
                ]
            )
            rows.append(
                [
                    button(
                        ("✅ " if tolerance == value else "") + f"±{value}%",
                        "set_tolerance",
                        client_id=client_id,
                        value=value,
                    )
                    for value in TOLERANCES
                ]
            )
            for field, label in TARGET_FIELDS.items():
                row(f"✏️ Задать {label}", "enter_target", client_id=client_id, field=field)
            row(
                "🚫 Не подсвечивать кампании (бренд и т. п.)",
                "enter_target",
                client_id=client_id,
                field="excluded_campaigns",
            )
            row("← К отчёту", "report", client_id=client_id, page=page)
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
            or monotonic() - item[0] > MENU_TTL
            or not callback.message
            or item[1] != callback.message.chat.id
            # Private chats stay bound to the opener; a group menu is shared.
            or (callback.message.chat.type == "private" and item[2] != callback.from_user.id)
        ):
            answered = await answer_callback(
                callback,
                "Кнопка из старого меню: бот перезапускался или меню истекло. Откройте /menu.",
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
        _, chat, _opener, action, kwargs, screen_id = item
        # In a group anyone may press a shared menu; rights are those of the presser.
        user = callback.from_user.id
        try:
            client_id = kwargs.get("client_id")
            if client_id:
                runtime.registry.require(chat, client_id)
            admin_only = action in (
                "schedule",
                "users",
                "member_add",
                "member_revoke",
                "member_restore",
                "member_clients",
                "member_toggle_client",
                "refresh",
            )
            configuration = action in (
                "data",
                "refresh_data",
                "counters",
                "toggle_counter",
                "counter_unavailable",
                "goals",
                "toggle_goal",
                "auto_goals",
                "kpi",
                "set_kpi",
                "set_tolerance",
                "enter_target",
            )
            if (admin_only and user not in runtime.settings.telegram_admin_user_ids) or (
                configuration and not can_configure(user)
            ):
                raise PermissionError
        except (PermissionError, KeyError):
            await answer_callback(
                callback, "Доступ больше не разрешён. Откройте /menu.", show_alert=True
            )
            return
        prune(screen_id)
        await answer_callback(callback)
        if action == "member_add":
            awaiting_user[(chat, user)] = monotonic()
            await callback.message.answer(
                "Пришлите числовой Telegram ID пользователя. "
                "Он получит доступ в личном чате ко всем клиентам, видимым вам здесь. /cancel — отмена."
            )
        elif action == "member_clients":
            await show(
                callback.message,
                user,
                screen="member_clients",
                client_id=kwargs["member_id"],
                page=kwargs.get("page", 0),
                edit=True,
            )
        elif action == "member_toggle_client":
            uid = kwargs["member_id"]
            member = (await runtime.checks.repository.bot_users()).get(uid)
            if member is not None:
                mine = set(member["client_ids"] or []) ^ {kwargs["target"]}
                await runtime.checks.repository.set_bot_user(
                    uid, member["enabled"], sorted(mine), user
                )
            await show(
                callback.message,
                user,
                screen="member_clients",
                client_id=uid,
                page=kwargs.get("page", 0),
                edit=True,
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
        elif action == "counter_unavailable":
            await callback.message.answer(
                f"Счётчик {kwargs['counter_id']} указан в кампаниях Директа, но текущий "
                "METRICA_OAUTH_TOKEN его не видит. Доступ к Директу не даёт доступ к "
                "Метрике автоматически. Владелец счётчика должен выдать аккаунту токена "
                "гостевой доступ на просмотр или редактирование, затем нажмите "
                "«Обновить доступы»."
            )
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
        elif action == "auto_goals":
            client = runtime.registry.require(chat, kwargs["client_id"])
            updated = await runtime.checks.reset_goals(client, user)
            if not updated.direct.main_goal_ids:
                await callback.message.answer(
                    "В Метрике нет избранных целей, а в кампаниях — заданных целей. "
                    "Отметьте 1–2 главные цели звёздочкой в Метрике или выберите их здесь."
                )
            await show(callback.message, user, screen="goals", edit=True, client_id=client.id)
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
        elif action in ("set_kpi", "set_tolerance"):
            client = runtime.registry.require(chat, kwargs["client_id"])
            field = "kpi" if action == "set_kpi" else "kpi_change_tolerance_percent"
            await save_targets(client, {field: kwargs["value"]}, user)
            await show(callback.message, user, screen="kpi", edit=True, client_id=client.id)
        elif action == "enter_target":
            awaiting_target[(chat, user)] = (monotonic(), kwargs["client_id"], kwargs["field"])
            await callback.message.answer(
                "Пришлите слова из названий кампаний через запятую, например: бренд, brand. "
                "Такие кампании останутся в итогах, но не будут попадать в риски. "
                "0 — убрать исключения, /cancel — отмена."
                if kwargs["field"] == "excluded_campaigns"
                else f"Пришлите {TARGET_FIELDS[kwargs['field']]} числом, например 2500. "
                "0 — убрать цель, /cancel — отмена."
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
