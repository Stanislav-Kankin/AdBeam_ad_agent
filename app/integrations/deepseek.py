from dataclasses import dataclass, field
from typing import Protocol

from openai import AsyncOpenAI


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str


@dataclass
class LLMMessage:
    content: str = ""
    calls: list[ToolCall] = field(default_factory=list)

    def as_dict(self):
        result = {"role": "assistant", "content": self.content or None}
        if self.calls:
            result["tool_calls"] = [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.name, "arguments": c.arguments},
                }
                for c in self.calls
            ]
        return result


class LLMProvider(Protocol):
    async def complete(self, messages: list[dict], tools: list[dict]) -> LLMMessage: ...


class DeepSeekProvider:
    def __init__(self, settings):
        self.client = AsyncOpenAI(
            api_key=settings.deepseek_api_key.get_secret_value(),
            base_url=settings.deepseek_base_url,
            timeout=45,
            max_retries=1,
        )
        self.model = settings.deepseek_model

    async def complete(self, messages, tools):
        result = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=tools,
            max_tokens=2500,
            temperature=0.2,
            extra_body={"thinking": {"type": "disabled"}},
        )
        msg = result.choices[0].message
        return LLMMessage(
            content=msg.content or "",
            calls=[
                ToolCall(c.id, c.function.name, c.function.arguments)
                for c in (msg.tool_calls or [])
            ],
        )

    async def close(self):
        await self.client.close()
