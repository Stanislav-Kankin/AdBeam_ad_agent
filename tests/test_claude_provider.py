import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pydantic import SecretStr

from app.agent.tools import tool_schemas
from app.integrations.claude import ClaudeProvider, to_anthropic, to_tools


def test_dialogue_converts_to_messages_api():
    system, turns = to_anthropic(
        [
            {"role": "system", "content": "Ты аналитик."},
            {"role": "system", "content": "Контекст: клиент West."},
            {"role": "user", "content": "Лучшая кампания?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "t1",
                        "type": "function",
                        "function": {"name": "list_clients", "arguments": "{}"},
                    },
                    {
                        "id": "t2",
                        "type": "function",
                        "function": {
                            "name": "get_campaign_goal_performance",
                            "arguments": '{"client_id":"west","days":120}',
                        },
                    },
                ],
            },
            {"role": "tool", "tool_call_id": "t1", "content": '{"clients":[]}'},
            {"role": "tool", "tool_call_id": "t2", "content": '{"status":"ok"}'},
        ]
    )
    assert system == "Ты аналитик.\n\nКонтекст: клиент West."
    assert [t["role"] for t in turns] == ["user", "assistant", "user"]
    uses = turns[1]["content"]
    assert uses[1] == {
        "type": "tool_use",
        "id": "t2",
        "name": "get_campaign_goal_performance",
        "input": {"client_id": "west", "days": 120},
    }
    # Both tool results travel in one user turn, as the API requires.
    assert [b["tool_use_id"] for b in turns[2]["content"]] == ["t1", "t2"]


def test_every_agent_tool_has_a_valid_schema():
    tools = to_tools(tool_schemas())
    assert tools and all(t["input_schema"]["type"] == "object" for t in tools)


async def test_answer_blocks_become_llm_message():
    settings = SimpleNamespace(
        anthropic_api_key=SecretStr("sk-ant-test-not-real"), anthropic_model="claude-sonnet-5"
    )
    provider = ClaudeProvider(settings)
    provider.client.messages.create = AsyncMock(
        return_value=SimpleNamespace(
            content=[
                SimpleNamespace(type="text", text="Проверяю."),
                SimpleNamespace(type="tool_use", id="u1", name="list_clients", input={"top_n": 5}),
            ]
        )
    )
    result = await provider.complete(
        [{"role": "system", "content": "s"}, {"role": "user", "content": "q"}],
        tool_schemas(),
    )
    assert result.content == "Проверяю."
    assert result.calls[0].name == "list_clients"
    assert json.loads(result.calls[0].arguments) == {"top_n": 5}
    kwargs = provider.client.messages.create.await_args.kwargs
    assert kwargs["model"] == "claude-sonnet-5"
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}
    await provider.close()


async def test_env_switch_selects_claude_and_falls_back_to_deepseek(settings):
    from app.integrations.deepseek import DeepSeekProvider
    from app.runtime import build_runtime

    claude = build_runtime(
        settings.model_copy(
            update={
                "llm_provider": "anthropic",
                "anthropic_api_key": SecretStr("sk-ant-test-not-real"),
                "deepseek_api_key": SecretStr("sk-test-not-real"),
            }
        )
    )
    assert isinstance(claude.agent.llm, ClaudeProvider)
    await claude.close()
    # Provider set to anthropic but no key yet: keep answering with DeepSeek.
    fallback = build_runtime(
        settings.model_copy(
            update={"llm_provider": "anthropic", "deepseek_api_key": SecretStr("sk-test-not-real")}
        )
    )
    assert isinstance(fallback.agent.llm, DeepSeekProvider)
    await fallback.close()
