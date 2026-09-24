import asyncio
import json
import logging
import re
from importlib.resources import files
from time import monotonic
from uuid import uuid4

from app.agent.tools import ToolRegistry, tool_schemas
from app.analytics.periods import make_period, today_moscow
from app.domain.reports import CheckMode, TriggerSource
from app.security import redact

logger = logging.getLogger(__name__)


class AgentService:
    def __init__(self, checks, llm=None, daily_limit=None):
        self.checks, self.llm = checks, llm
        self.tools = ToolRegistry(checks)
        self.daily_limit = daily_limit

    async def cancel(self, chat_id, user_id):
        await self.checks.repository.clear_conversation(chat_id, user_id)

    async def ask(self, text, chat_id, user_id):
        if chat_id not in self.checks.registry.allowed_chats:
            raise PermissionError("Чат не разрешён.")
        if text.strip().casefold().rstrip(".!\\ ") in ("проверка связи", "пинг", "ping"):
            return "На связи. Выберите клиента в /menu или напишите, какой отчёт нужен."
        if not self.llm:
            return await self.deterministic_fallback(
                text,
                chat_id,
                "DeepSeek не подключён. Для свободного анализа настройте DEEPSEEK_API_KEY в .env.",
                user_id=user_id,
            )
        request_id, start = str(uuid4()), monotonic()
        context = await self.checks.repository.conversation(chat_id, user_id)
        old = context["messages"]
        system = files("app.agent").joinpath("system_prompt.txt").read_text(encoding="utf-8")
        system += f"\nСегодня по Москве: {today_moscow()}. Режим: {'MOCK, синтетические данные' if self.checks.provider.mock else 'production'}."
        active_id = context.get("active_client_id")
        if active_id:
            try:
                active = self.checks.registry.require(chat_id, active_id)
                system += f"\nТекущий контекст беседы: клиент {active.name}, ID {active.id}."
            except PermissionError:
                active_id = None
        if context.get("period"):
            system += "\nПоследний использованный период: " + json.dumps(
                context["period"], ensure_ascii=False
            )
        messages = [
            {"role": "system", "content": system},
            *old[-6:],
            {"role": "user", "content": redact(text)[:4000]},
        ]
        count, evidence = 0, []
        active_period = context.get("period")
        phase = "llm"
        analytics_started = False
        try:
            async with asyncio.timeout(240):
                while count <= 8:
                    phase = "llm"
                    if (
                        self.daily_limit is not None
                        and not await self.checks.repository.reserve_model_call(
                            chat_id, user_id, request_id, self.daily_limit
                        )
                    ):
                        return self.fallback(
                            evidence,
                            "Достигнут суточный лимит обращений к DeepSeek для этого чата. Обычные отчёты /check и /summary доступны.",
                        )
                    logger.info("Agent model started request=%s", request_id)
                    async with asyncio.timeout(45):
                        reply = await self.llm.complete(messages, tool_schemas())
                    logger.info(
                        "Agent model finished request=%s calls=%s", request_id, len(reply.calls)
                    )
                    if not reply.calls:
                        answer = (
                            redact(reply.content)
                            or "Не удалось получить ответ. Используйте /check <клиент>."
                        )
                        if self.checks.provider.mock:
                            answer = "🧪 MOCK — тестовые данные\n" + answer
                        await self.checks.repository.save_conversation(
                            chat_id,
                            user_id,
                            [
                                *old[-4:],
                                {"role": "user", "content": redact(text)[:2000]},
                                {"role": "assistant", "content": answer[:4000]},
                            ],
                            active_client_id=active_id,
                            period=active_period,
                        )
                        return answer
                    if count + len(reply.calls) > 8:
                        return self.fallback(
                            evidence,
                            "Достигнут лимит 8 инструментов. Уточните запрос или используйте /check.",
                        )
                    messages.append(reply.as_dict())
                    for call in reply.calls:
                        count += 1
                        phase = "tool"
                        analytics_started |= call.name != "list_clients"
                        result = await self.tools.call(
                            call.name,
                            call.arguments,
                            chat_id=chat_id,
                            request_id=request_id,
                            user_id=user_id,
                        )
                        evidence.append(result)
                        if result.get("client_id"):
                            active_id = result["client_id"]
                        if result.get("period"):
                            active_period = result["period"]
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.id,
                                "content": json.dumps(result, ensure_ascii=False),
                            }
                        )
                return self.fallback(evidence, "Достигнут лимит инструментов.")
        except Exception as exc:
            logger.warning(
                "Agent stopped request=%s phase=%s error=%s elapsed=%.1fs",
                request_id,
                phase,
                type(exc).__name__,
                monotonic() - start,
            )
            await self.checks.repository.tool_event(
                request_id=request_id,
                chat_id=str(chat_id),
                user_id=str(user_id),
                tool="llm_provider",
                client_id=None,
                arguments={},
                status="error",
                error=f"{phase}: {type(exc).__name__}",
                duration_seconds=monotonic() - start,
            )
            message = (
                "Сбор данных не завершился в отведённое время. Повторная проверка автоматически не запускается."
                if phase == "tool"
                else "DeepSeek недоступен или не ответил вовремя. Показываю доступный результат без комментария модели."
            )
            if any(result.get("reports") for result in evidence):
                return self.fallback(evidence, message)
            if analytics_started:
                return (
                    message
                    + "\nГотового отчёта нет. Используйте /summary <клиент> для краткой проверки."
                )
            return await self.deterministic_fallback(text, chat_id, message, user_id=user_id)

    async def explain_reports(self, reports, deterministic_text, chat_id, user_id=None):
        """Use the model as an editor over backend-calculated facts, never as a calculator."""
        if len(reports) == 1:
            return await self.explain_card(reports[0], chat_id, user_id)
        if not self.llm or not reports:
            return deterministic_text, False
        request_id = str(uuid4())
        if self.daily_limit is not None and not await self.checks.repository.reserve_model_call(
            chat_id, user_id, request_id, self.daily_limit
        ):
            return deterministic_text, False
        system = (
            "Ты редактор аналитического отчёта AdBeam. Используй только факты и числа из "
            "переданного готового отчёта. Не пересчитывай показатели, не добавляй причины как "
            "факты. Подготовь первый управленческий ответ не длиннее 1000 знаков и ровно из "
            "двух абзацев. В первом: статус, период и главный KPI. Во втором: главное "
            "изменение и одно следующее действие. Не перечисляй все доступные "
            "показатели, цели и технические ошибки. Не повторяй одну мысль в разных разделах. "
            "Текущий статус кампании не используй как причину изменения в прошлом без даты "
            "смены статуса. Гипотезу явно называй гипотезой. Названия показателей и значения "
            "выделяй Markdown-жирным. Не используй Markdown-таблицы."
        )
        prompt = "Готовый отчёт backend:\n\n" + deterministic_text
        try:
            async with asyncio.timeout(45):
                reply = await self.llm.complete(
                    [
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt[:60000]},
                    ],
                    [],
                )
            if reply.calls or not reply.content.strip():
                return deterministic_text, False
            answer = redact(reply.content.strip())
            active_client_id = reports[0].client_id if len(reports) == 1 else None
            await self.checks.repository.save_conversation(
                chat_id,
                user_id,
                [
                    {"role": "user", "content": "Подготовь аналитический отчёт."},
                    {"role": "assistant", "content": answer[:4000]},
                ],
                active_client_id=active_client_id,
                period=reports[0].period.model_dump(mode="json") if reports else None,
            )
            return answer, True
        except Exception as exc:
            logger.warning("Report narration failed error=%s", type(exc).__name__)
            return deterministic_text, False

    async def explain_card(self, report, chat_id, user_id=None):
        """The card structure is fixed; the model only writes its short conclusion."""
        from app.reporting.formatter import card

        text = card(report)
        if not self.llm:
            return text, True
        request_id = str(uuid4())
        if self.daily_limit is not None and not await self.checks.repository.reserve_model_call(
            chat_id, user_id, request_id, self.daily_limit
        ):
            return text, True
        system = (
            "Ты старший performance-аналитик агентства. По готовой карточке клиента напиши "
            "вывод для специалиста: 1–2 предложения, не длиннее 300 знаков. Используй только "
            "факты и числа из карточки, ничего не пересчитывай. Сначала состояние главного KPI, "
            "затем главное, что на него повлияло или что требует внимания. Изменения с пометкой "
            "«стабильно» не акцентируй. Гипотезу называй гипотезой. Без Markdown, списков и "
            "заголовков. Не повторяй список показателей."
        )
        try:
            async with asyncio.timeout(45):
                reply = await self.llm.complete(
                    [
                        {"role": "system", "content": system},
                        {"role": "user", "content": text[:16000]},
                    ],
                    [],
                )
            summary = redact(reply.content.strip())
            if reply.calls or not summary or len(summary) > 500:
                return text, True
            answer = card(report, summary=summary)
            await self.checks.repository.save_conversation(
                chat_id,
                user_id,
                [
                    {"role": "user", "content": "Подготовь аналитический отчёт."},
                    {"role": "assistant", "content": answer[:4000]},
                ],
                active_client_id=report.client_id,
                period=report.period.model_dump(mode="json"),
            )
            return answer, True
        except Exception as exc:
            logger.warning("Report narration failed error=%s", type(exc).__name__)
            return text, True

    async def explain_daily_digest(self, deterministic_text, chat_id):
        """Polish a compact scheduled digest without expanding it into a full report."""
        if not self.llm:
            return deterministic_text, False
        request_id = str(uuid4())
        if self.daily_limit is not None and not await self.checks.repository.reserve_model_call(
            chat_id, None, request_id, self.daily_limit
        ):
            return deterministic_text, False
        system = (
            "Ты выпускающий редактор ежедневного отчёта рекламного агентства. "
            "Используй только факты из готового дайджеста. Сохрани его коротким: до 2500 знаков. "
            "Не перечисляй все метрики и все технические ограничения. Не повторяй одну мысль в "
            "разных разделах. Различай отсутствие рекламной активности и ошибку API. "
            "Структура: главный вывод; требует внимания; что сделать сегодня; строка о полноте "
            "данных. Не используй Markdown-таблицы. Названия проектов и ключевые цифры можно "
            "выделять Markdown-жирным."
        )
        try:
            async with asyncio.timeout(45):
                reply = await self.llm.complete(
                    [
                        {"role": "system", "content": system},
                        {"role": "user", "content": deterministic_text[:16000]},
                    ],
                    [],
                )
            answer = redact(reply.content.strip())
            if reply.calls or not answer or len(answer) > 3500:
                return deterministic_text, False
            return answer, True
        except Exception as exc:
            logger.warning("Daily narration failed error=%s", type(exc).__name__)
            return deterministic_text, False

    async def deterministic_fallback(self, text, chat_id, message, *, user_id=None):
        question = text.casefold()
        clients = self.checks.registry.visible(chat_id)
        selected = [
            c
            for c in clients
            if any(
                re.search(r"(?<!\w)" + re.escape(v.casefold()) + r"(?!\w)", question)
                for v in [c.id, c.name, *c.aliases]
            )
        ]
        all_clients = any(v in question for v in ("всем клиентам", "всех клиентов", "все клиенты"))
        if all_clients:
            selected = clients
        if not selected:
            context = await self.checks.repository.conversation(chat_id, user_id)
            active_id = context.get("active_client_id")
            if active_id:
                try:
                    selected = [self.checks.registry.require(chat_id, active_id)]
                except PermissionError:
                    selected = []
        if not selected or len(selected) > 1 and not all_clients:
            return (
                message
                + "\nУкажите клиента и период командой /check <клиент> [7d]. /clients — список."
            )
        # A fallback must not silently reinterpret explicit/custom/incomplete dates.
        if re.search(r"\d{4}-\d{2}-\d{2}|\d{1,2}[./]\d{1,2}", question) or any(
            v in question
            for v in (
                "сегодня",
                "текущ",
                "январ",
                "феврал",
                "март",
                "апрел",
                "мая",
                "июн",
                "июл",
                "август",
                "сентябр",
                "октябр",
                "ноябр",
                "декабр",
                "позавчера",
            )
        ):
            return (
                message
                + "\nДля этого периода используйте /check с 1d–90d или повторите вопрос позже."
            )
        match = re.search(r"\b(\d{1,3})\s*(?:d\b|дн|дней)", question)
        month_match = re.search(r"\b([1-3])\s*(?:месяц|месяца|месяцев|month|months)", question)
        period_name = (
            f"{match[1]}d"
            if match
            else f"{int(month_match[1]) * 30}d"
            if month_match
            else "yesterday"
            if "вчера" in question
            else "7d"
        )
        try:
            reports, report = await self.checks.run_check(
                [c.id for c in selected],
                make_period(period_name),
                CheckMode.STANDARD,
                TriggerSource.AGENT,
                chat_id=chat_id,
                user_id=user_id,
            )
            if len(selected) == 1:
                await self.checks.repository.save_conversation(
                    chat_id,
                    user_id,
                    [
                        *(await self.checks.repository.conversation(chat_id, user_id))["messages"][
                            -4:
                        ],
                        {"role": "user", "content": redact(text)[:2000]},
                        {"role": "assistant", "content": report[:4000]},
                    ],
                    active_client_id=selected[0].id,
                    period=reports[0].period.model_dump(mode="json") if reports else None,
                )
            return message + "\nДетерминированная стандартная проверка:\n\n" + report
        except Exception:
            return message + "\nНе удалось завершить проверку. Используйте /check <клиент> позже."

    @staticmethod
    def fallback(evidence, message):
        from app.domain.reports import ClientReport
        from app.reporting.formatter import detailed

        reports = [r for result in evidence for r in result.get("reports", [])]
        if reports:
            return (
                message
                + "\n\n"
                + "\n\n".join(detailed(ClientReport.model_validate(r)) for r in reports)
            )
        return message
