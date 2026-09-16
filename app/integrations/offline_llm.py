"""Deterministic demonstration of the tool protocol, never used in production."""

import json
import re

from app.domain.reports import ClientReport
from app.integrations.deepseek import LLMMessage, ToolCall
from app.reporting.formatter import detailed


class OfflineDemoProvider:
    async def complete(self, messages, tools):
        last_user = max(i for i, m in enumerate(messages) if m["role"] == "user")
        question = messages[last_user]["content"].casefold()
        results = [
            json.loads(m["content"]) for m in messages[last_user + 1 :] if m["role"] == "tool"
        ]
        if not results:
            return LLMMessage(calls=[ToolCall("demo_list", "list_clients", "{}")])
        clients = results[0].get("clients", [])
        selected = [
            c
            for c in clients
            if any(x.casefold() in question for x in [c["id"], c["name"], *c["aliases"]])
        ]
        all_clients = any(
            word in question for word in ["всем", "всех", "все клиенты", "каких клиентов"]
        )
        if all_clients:
            selected = clients[:6]
        if not selected or (len(selected) > 1 and not all_clients):
            return LLMMessage(
                "Демонстрационный режим без LLM. Какого клиента проверить?\n"
                + "\n".join(c["name"] for c in (selected or clients))
            )
        match = re.search(r"\b(\d{1,2})\s*(?:d\b|дн|дней)", question)
        period = f"{match[1]}d" if match else "yesterday" if "вчера" in question else "7d"
        if len(results) <= len(selected):
            client = selected[len(results) - 1]
            return LLMMessage(
                calls=[
                    ToolCall(
                        f"demo_{len(results)}",
                        "get_account_overview",
                        json.dumps({"client_id": client["id"], "period": period}),
                    )
                ]
            )
        reports = [r for result in results for r in result.get("reports", [])]
        text = "Демонстрационный разбор без DeepSeek.\n\n" + "\n\n".join(
            detailed(ClientReport.model_validate(r)) for r in reports
        )
        if not reports:
            text += "Данные не получены. Проверьте период и клиента."
        return LLMMessage(text)
