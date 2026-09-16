import asyncio
import json
from importlib.resources import files
from time import monotonic
from uuid import uuid4

from app.agent.tools import ToolRegistry, tool_schemas
from app.analytics.periods import today_moscow
from app.security import redact


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
        if not self.llm:
            return "DeepSeek не подключён. Используйте /check <клиент>, /check_all или /summary_all. Для свободных вопросов настройте DEEPSEEK_API_KEY в .env."
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
        try:
            async with asyncio.timeout(240):
                while count <= 8:
                    reply = await self.llm.complete(messages, tool_schemas())
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
        except Exception:
            await self.checks.repository.tool_event(
                request_id=request_id,
                chat_id=str(chat_id),
                tool="llm_provider",
                client_id=None,
                arguments={},
                status="error",
                error="LLM unavailable",
                duration_seconds=monotonic() - start,
            )
            return self.fallback(
                evidence,
                "DeepSeek недоступен. Обычные отчёты /check и /summary продолжают работать.",
            )

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
