"""Tools managers asked for after the first demo: hiding a risk known to be fine,
rating answers, a daily question limit and a visible client context."""

from datetime import UTC, datetime

from aiogram import Bot
from aiogram.methods import EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Update, User

from app.bot.handlers import build_dispatcher
from tests.test_bot_schedule import FakeTelegram, message_update


class Chat:
    """A private chat with the bot: send text, press buttons by their label."""

    def __init__(self, runtime, bot, session, user=1):
        self.runtime, self.bot, self.session, self.user = runtime, bot, session, user
        self.dp = build_dispatcher(runtime)

    async def say(self, text):
        await self.dp.feed_update(self.bot, message_update(text, user=self.user))
        await self.runtime.jobs.close()

    def texts(self):
        return [
            m.text or "" for m in self.session.sent if isinstance(m, SendMessage | EditMessageText)
        ]

    def labels(self):
        return [
            button.text
            for message in self.session.sent
            if getattr(message, "reply_markup", None) is not None
            for row in message.reply_markup.inline_keyboard
            for button in row
        ]

    async def press(self, label):
        data = next(
            button.callback_data
            for message in reversed(self.session.sent)
            if getattr(message, "reply_markup", None) is not None
            for row in message.reply_markup.inline_keyboard
            for button in row
            if label in button.text
        )
        await self.dp.feed_update(
            self.bot,
            Update(
                update_id=2,
                callback_query=CallbackQuery(
                    id="press",
                    from_user=User(id=self.user, is_bot=False, first_name="U"),
                    chat_instance="test",
                    message=message_update("menu", user=self.user).message,
                    data=data,
                ),
            ),
        )
        await self.runtime.jobs.close()


async def test_hidden_risk_leaves_the_card_for_a_week_and_comes_back(runtime):
    session = FakeTelegram()
    async with Bot(token="555:THIS_IS_A_SYNTHETIC_TEST_TOKEN", session=session) as bot:
        chat = Chat(runtime, bot, session)
        await chat.say('/check "West Экспорт" 7d')
        card = next(text for text in chat.texts() if "Требует внимания" in text)
        assert "РСЯ — каталог" in card
        await chat.press("Скрыть риск")
        assert any("РСЯ — каталог" in label for label in chat.labels())
        await chat.press("🔕 РСЯ — каталог")
        assert "Скрыто до" in chat.texts()[-1]
        assert [
            item["label"] for item in await runtime.checks.repository.snoozed_risks("west_export")
        ] == ["РСЯ — каталог"]

        before = len(chat.texts())
        await chat.say('/check "West Экспорт" 7d')
        card = next(text for text in chat.texts()[before:] if "Требует внимания" in text)
        risks = card.split("Требует внимания")[-1].split("\n\n")[0]
        assert "РСЯ — каталог" not in risks
        assert "Скрыты специалистом: «РСЯ — каталог»" in card

        await chat.say("/menu")
        await chat.press("Клиенты")
        await chat.press("West")
        await chat.press("KPI проекта")
        assert "Скрытые риски" in session.sent[-1].text
        await chat.press("Вернуть: РСЯ — каталог")
        assert await runtime.checks.repository.snoozed_risks("west_export") == []


async def test_risks_cannot_be_hidden_by_people_without_settings_rights(runtime):
    session = FakeTelegram()
    async with Bot(token="555:THIS_IS_A_SYNTHETIC_TEST_TOKEN", session=session) as bot:
        # A member of the allowed chat from .env, not added in the bot.
        chat = Chat(runtime, bot, session, user=7)
        await chat.say('/check "West Экспорт" 7d')
        assert not any("Скрыть риск" in label for label in chat.labels())


async def test_answer_is_rated_commented_and_client_switched(runtime):
    await runtime.checks.repository.save_conversation(
        123456789, 1, [], active_client_id="west_export"
    )
    session = FakeTelegram()
    async with Bot(token="555:THIS_IS_A_SYNTHETIC_TEST_TOKEN", session=session) as bot:
        chat = Chat(runtime, bot, session)
        await chat.say("Что с расходом West Экспорт за 7 дней?")
        assert "👍 Полезно" in chat.labels()
        assert "🔄 Сменить клиента · сейчас West Экспорт" in chat.labels()
        await chat.press("👎 Не то")
        assert "Что было не так?" in chat.texts()[-1]
        await chat.say("цифры расхода не сходятся с кабинетом")
        assert "Спасибо, записал" in chat.texts()[-1]
        stored = await runtime.checks.repository.feedback()
        assert stored[0]["rating"] == -1 and stored[0]["client_id"] == "west_export"
        assert stored[0]["comment"] == "цифры расхода не сходятся с кабинетом"

        await chat.say("/feedback")
        assert "👎" in chat.texts()[-1] and "не сходятся с кабинетом" in chat.texts()[-1]

        await chat.press("Сменить клиента")
        context = await runtime.checks.repository.conversation(123456789, 1)
        assert context["active_client_id"] is None
        assert "Подключённые клиенты" in session.sent[-1].text


async def test_command_after_thumbs_down_is_not_taken_as_a_comment(runtime):
    session = FakeTelegram()
    async with Bot(token="555:THIS_IS_A_SYNTHETIC_TEST_TOKEN", session=session) as bot:
        chat = Chat(runtime, bot, session)
        await chat.say('/check "West Экспорт" 7d')
        await chat.press("👎 Не то")
        await chat.say("/cancel")
        assert "контекст диалога очищен" in chat.texts()[-1]
        assert (await runtime.checks.repository.feedback())[0]["comment"] is None


async def test_free_questions_are_limited_per_person_but_not_for_admins(runtime):
    runtime.settings.questions_per_user_daily = 1
    await runtime.checks.repository.set_bot_user(2, True, ["west_export"], 1)
    session = FakeTelegram()
    async with Bot(token="555:THIS_IS_A_SYNTHETIC_TEST_TOKEN", session=session) as bot:
        manager = Chat(runtime, bot, session, user=2)
        await manager.say("Что с расходом West Экспорт?")
        assert "лимит" not in manager.texts()[-1]
        await manager.say("А с конверсиями?")
        assert "лимит вопросов исчерпан (1)" in manager.texts()[-1]
        admin = Chat(runtime, bot, session, user=1)
        await admin.say("А с конверсиями?")
        await admin.say("А с CPA?")
        assert "лимит" not in admin.texts()[-1]


async def test_model_spend_is_split_by_person(runtime):
    from app.reporting.balance import balance_text

    repo = runtime.checks.repository
    await repo.set_bot_user(42, True, [], 1)
    await repo.remember_user_name(42, "sveta_pm", "Света")
    assert await repo.reserve_model_call(42, 42, "request-1", 100)
    await repo.record_llm_usage("anthropic", "model", {"input": 10, "output": 5}, 0.25)
    assert await repo.llm_spend_by_user(datetime(2000, 1, 1, tzinfo=UTC)) == [("42", 0.25, 1)]
    text = await balance_text(runtime)
    assert "По людям с начала месяца:" in text and "Света · 42: $0,25" in text


def test_clients_are_found_in_any_case_form_but_not_by_a_lone_common_word():
    from types import SimpleNamespace

    from app.agent.mentions import mentioned

    def client(id_, name, login, aliases=()):
        return SimpleNamespace(
            id=id_, name=name, aliases=list(aliases), direct=SimpleNamespace(client_login=login)
        )

    clients = [
        client("bacon", "Тамбовский Бекон", "tambov-bekon"),
        client("grand", "Grand Line", "ab-grandline"),
        client("phos", "Phosagro", "ab-phosagro"),
        client("aroma", "Арома-Бутик", "aroma-butik"),
    ]
    cases = {
        "на какую гео ориентированы кампании у клиента Тамбовский Бекон?": ["bacon"],
        "что у Тамбовского бекона с CPA": ["bacon"],
        "а у фосагро?": ["phos"],
        "сравни Гранд и Фосагро": ["grand", "phos"],
        "отчёт по ab-grandline": ["grand"],
        "какой бутик лучше": [],
        "а с конверсиями?": [],
    }
    for text, expected in cases.items():
        assert mentioned(clients, text) == expected, text


async def test_question_about_another_client_switches_and_forgets(runtime):
    repo = runtime.checks.repository
    await repo.save_conversation(
        123456789,
        1,
        [{"role": "user", "content": "секрет про West"}],
        active_client_id="west_export",
    )
    session = FakeTelegram()
    async with Bot(token="555:THIS_IS_A_SYNTHETIC_TEST_TOKEN", session=session) as bot:
        chat = Chat(runtime, bot, session)
        await chat.say("Какой расход у Fresh Parfum за 7 дней?")
        assert any("Переключился на «Fresh Parfum" in text for text in chat.texts())
        context = await repo.conversation(123456789, 1)
        assert context["active_client_id"] == "fresh_parfum"
        assert "секрет про West" not in str(context["messages"])

        # A follow-up without a name, the same client or two clients: no switch.
        before = len(chat.texts())
        for text in ("А с конверсиями?", "Fresh Parfum: а CPA?", "Сравни West и Fresh Parfum"):
            await chat.say(text)
        assert not any("Переключился" in text for text in chat.texts()[before:])


async def test_model_gets_the_current_client_list_with_every_question(runtime):
    from app.integrations.deepseek import LLMMessage

    seen = []

    class Recorder:
        async def complete(self, messages, tools):
            seen.append(messages[0]["content"])
            return LLMMessage(content="Готово.")

    runtime.agent.llm = Recorder()
    await runtime.agent.ask("Что нового?", 123456789, 1)
    assert "Доступные пользователю клиенты сейчас" in seen[0]
    assert "Fresh Parfum (логин example-fresh)" in seen[0]


async def test_out_of_tool_calls_the_model_answers_from_what_it_gathered(runtime):
    from app.integrations.deepseek import LLMMessage, ToolCall

    class Greedy:
        """Asks for three reports at a time until told the calls are over."""

        def __init__(self, obey=True):
            self.obey, self.calls = obey, 0

        async def complete(self, messages, tools):
            told = "исчерпан" in str(messages[-1].get("content"))
            if told and self.obey:
                return LLMMessage(content="Итог по собранным данным; не хватило аудитории.")
            self.calls += 1
            return LLMMessage(
                calls=[ToolCall(f"c{self.calls}{i}", "list_clients", "{}") for i in range(3)]
            )

    runtime.agent.llm = Greedy()
    answer = await runtime.agent.ask("Полный анализ West за октябрь", 123456789, 1)
    assert "Итог по собранным данным" in answer and "лимит" not in answer.casefold()

    runtime.agent.llm = Greedy(obey=False)
    answer = await runtime.agent.ask("Полный анализ West за октябрь", 123456789, 1)
    assert "сузьте вопрос" in answer
