"""Anthropic Claude behind the same LLMProvider interface as DeepSeek.

The agent keeps its dialogue in the OpenAI chat format; this adapter converts it to
the Messages API (system prompt apart, tool calls as tool_use blocks, tool results as
tool_result blocks inside user turns) and converts the answer back.
"""

import json
import logging

from anthropic import AsyncAnthropic, BadRequestError

from app.integrations.deepseek import LLMMessage, ToolCall

logger = logging.getLogger(__name__)


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
        self.prices = (
            settings.anthropic_price_input,
            settings.anthropic_price_cache_write,
            settings.anthropic_price_cache_read,
            settings.anthropic_price_output,
        )
        self.usage_sink = None
        # Hidden reasoning shares max_tokens with the answer and cut replies mid-word;
        # the backend computes every number, so the model does not need it.
        self.thinking = {"type": "disabled"}

    async def complete(self, messages, tools):
        system, turns = to_anthropic(messages)
        if turns:
            # Each agent step resends the whole dialogue: cache it up to the newest
            # block, so the next step pays a tenth for everything before it.
            turns[-1]["content"][-1] = {
                **turns[-1]["content"][-1],
                "cache_control": {"type": "ephemeral"},
            }
        request = {
            "model": self.model,
            "max_tokens": 4000,
            # The tool list and system prompt repeat on every call: cache them too.
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "tools": to_tools(tools),
            "messages": turns,
        }
        try:
            result = await self.client.messages.create(
                **request, **({"thinking": self.thinking} if self.thinking else {})
            )
        except BadRequestError as exc:
            if not self.thinking or "thinking" not in str(exc).lower():
                raise
            logger.warning("Model does not accept disabled thinking; retrying without it")
            self.thinking = None
            result = await self.client.messages.create(**request)
        usage = result.usage
        logger.info(
            "Claude usage model=%s stop=%s input=%s cache_read=%s cache_write=%s output=%s",
            self.model,
            result.stop_reason,
            usage.input_tokens,
            getattr(usage, "cache_read_input_tokens", None),
            getattr(usage, "cache_creation_input_tokens", None),
            usage.output_tokens,
        )
        tokens = {
            "input": usage.input_tokens or 0,
            "cache_write": getattr(usage, "cache_creation_input_tokens", None) or 0,
            "cache_read": getattr(usage, "cache_read_input_tokens", None) or 0,
            "output": usage.output_tokens or 0,
        }
        if self.usage_sink:
            price_in, price_write, price_read, price_out = self.prices
            cost = (
                tokens["input"] * price_in
                + tokens["cache_write"] * price_write
                + tokens["cache_read"] * price_read
                + tokens["output"] * price_out
            ) / 1_000_000
            try:
                await self.usage_sink("anthropic", self.model, tokens, cost)
            except Exception:
                logger.exception("Could not record model usage")
        text = "".join(block.text for block in result.content if block.type == "text")
        if result.stop_reason == "max_tokens" and text:
            text = text.rstrip() + "…\n\nОтвет не поместился целиком — уточните вопрос."
        return LLMMessage(
            content=text,
            calls=[
                ToolCall(block.id, block.name, json.dumps(block.input, ensure_ascii=False))
                for block in result.content
                if block.type == "tool_use"
            ],
        )

    async def close(self):
        await self.client.close()
