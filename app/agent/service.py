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
    def __init__(self, checks, llm=None):
        self.checks, self.llm = checks, llm
        self.tools = ToolRegistry(checks)
        self.history = {}

    def cancel(self, chat_id, user_id):
        self.history.pop((chat_id, user_id), None)

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
            )
        request_id, start = str(uuid4()), monotonic()
        key = (chat_id, user_id)
        # Drop old conversations and bound memory by both sessions and message length.
        self.history = {k: v for k, v in self.history.items() if monotonic() - v[0] < 1800}
        if len(self.history) >= 200:
            self.history.pop(min(self.history, key=lambda k: self.history[k][0]))
        old = self.history.get(key, (0, []))[1]
        system = files("app.agent").joinpath("system_prompt.txt").read_text(encoding="utf-8")
        system += f"\nСегодня по Москве: {today_moscow()}. Режим: {'MOCK, синтетические данные' if self.checks.provider.mock else 'production'}."
        messages = [
            {"role": "system", "content": system},
            *old[-6:],
            {"role": "user", "content": redact(text)[:4000]},
        ]
        count, evidence = 0, []
        phase = "llm"
        analytics_started = False
        try:
            async with asyncio.timeout(240):
                while count <= 8:
                    phase = "llm"
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
                        self.history[key] = (
                            monotonic(),
                            [
                                *old[-4:],
                                {"role": "user", "content": redact(text)[:2000]},
                                {"role": "assistant", "content": answer[:4000]},
                            ],
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
                            call.name, call.arguments, chat_id=chat_id, request_id=request_id
                        )
                        evidence.append(result)
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
            return await self.deterministic_fallback(text, chat_id, message)

    async def deterministic_fallback(self, text, chat_id, message):
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
        period_name = f"{match[1]}d" if match else "yesterday" if "вчера" in question else "7d"
        try:
            _, report = await self.checks.run_check(
                [c.id for c in selected],
                make_period(period_name),
                CheckMode.STANDARD,
                TriggerSource.AGENT,
                chat_id=chat_id,
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
