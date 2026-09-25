"""Anthropic Claude behind the same LLMProvider interface as DeepSeek.

The agent keeps its dialogue in the OpenAI chat format; this adapter converts it to
the Messages API (system prompt apart, tool calls as tool_use blocks, tool results as
tool_result blocks inside user turns) and converts the answer back.
"""

import json

from anthropic import AsyncAnthropic

from app.integrations.deepseek import LLMMessage, ToolCall


def to_anthropic(messages):
    system, turns = [], []

    def push(role, blocks):
        # The Messages API wants alternating roles; merge consecutive turns.
        if turns and turns[-1]["role"] == role:
            turns[-1]["content"].extend(blocks)
        else:
            turns.append({"role": role, "content": blocks})

    for message in messages:
        role = message["role"]
        if role == "system":
            system.append(message["content"])
        elif role == "user":
            push("user", [{"type": "text", "text": message["content"] or "…"}])
        elif role == "assistant":
            blocks = (
                [{"type": "text", "text": message["content"]}] if message.get("content") else []
            )
            for call in message.get("tool_calls") or []:
                try:
                    arguments = json.loads(call["function"]["arguments"] or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call["id"],
                        "name": call["function"]["name"],
                        "input": arguments if isinstance(arguments, dict) else {},
                    }
                )
            if blocks:
                push("assistant", blocks)
        elif role == "tool":
            push(
                "user",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": message["tool_call_id"],
                        "content": message["content"]
                        if isinstance(message["content"], str)
                        else json.dumps(message["content"], ensure_ascii=False),
                    }
                ],
            )
    return "\n\n".join(system), turns


def to_tools(tools):
    return [
        {
            "name": tool["function"]["name"],
            "description": tool["function"]["description"],
            "input_schema": tool["function"]["parameters"],
        }
        for tool in tools
    ]


class ClaudeProvider:
    name = "Claude"

    def __init__(self, settings):
        self.client = AsyncAnthropic(
            api_key=settings.anthropic_api_key.get_secret_value(), timeout=60, max_retries=1
        )
        self.model = settings.anthropic_model

    async def complete(self, messages, tools):
        system, turns = to_anthropic(messages)
        result = await self.client.messages.create(
            model=self.model,
            max_tokens=2500,
            # The system prompt and tool list repeat on every call: cache them.
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            tools=to_tools(tools),
            messages=turns,
        )
        return LLMMessage(
            content="".join(block.text for block in result.content if block.type == "text"),
            calls=[
                ToolCall(block.id, block.name, json.dumps(block.input, ensure_ascii=False))
                for block in result.content
                if block.type == "tool_use"
            ],
        )

    async def close(self):
        await self.client.close()
