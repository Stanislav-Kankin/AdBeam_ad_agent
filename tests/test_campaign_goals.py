from decimal import Decimal
from unittest.mock import AsyncMock

import httpx

from app.agent.tools import ToolRegistry
from app.integrations.direct import DirectAdapter, parse_goal_tsv, strategy_goals
from app.integrations.http import ReadTransport

GOAL_TSV = (
    "CampaignId\tCampaignName\tImpressions\tClicks\tCost"
    "\tConversions_5001_LSC\tConversions_5002_LSC\n"
    "101\tПоиск\t1000\t100\t5000\t10\t--\n"
)


def test_goal_tsv_keeps_conversions_per_goal():
    rows = parse_goal_tsv(GOAL_TSV, ["5001", "5002"], "LSC")
    assert rows["101"]["goals"] == {"5001": Decimal(10), "5002": Decimal(0)}
    assert rows["101"]["spend"] == Decimal(5000)


def test_strategy_goal_ignores_direct_placeholders():
    strategy = {
        "Search": {"WbMaximumConversionRate": {"GoalId": 7001, "WeeklySpendLimit": 1}},
        "Network": {"AverageCpa": {"GoalId": 13}},  # 13 = "key goals" placeholder
    }
    assert strategy_goals(strategy) == ["7001"]


async def test_direct_reads_goals_set_in_campaigns(client, monkeypatch):
    monkeypatch.setenv("DIRECT_OAUTH_TOKEN", "test-token-not-real")

    def handler(request):
        body = request.read().decode()
        assert '"PriorityGoals"' in body and '"method": "get"' in body.replace('":"', '": "')
        return httpx.Response(
            200,
            json={
                "result": {
                    "Campaigns": [
                        {
                            "Id": 101,
                            "Name": "Вакансии",
                            "State": "ON",
                            "Type": "TEXT_CAMPAIGN",
                            "TextCampaign": {
                                "PriorityGoals": {"Items": [{"GoalId": 5001, "Value": 100}]},
                                "BiddingStrategy": {"Search": {"AverageCpa": {"GoalId": 5002}}},
                            },
                        }
                    ]
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        goals = await DirectAdapter(ReadTransport(http)).campaign_goals(client)
    assert goals["101"]["goal_ids"] == ["5001", "5002"]
    assert goals["101"]["priority_goal_ids"] == ["5001"]
    assert goals["101"]["primary_goal_id"] == "5002"  # the strategy goal wins


async def test_tool_ranks_campaigns_by_their_own_goals_over_four_months(runtime, client):
    # Rusagro case: no main goals selected, 4 months requested, goals live in campaigns.
    client.direct.main_goal_ids, client.metrica.main_goal_ids = [], []
    spy = AsyncMock(wraps=runtime.checks.provider.goal_report)
    runtime.checks.provider.goal_report = spy
    registry = ToolRegistry(runtime.checks)
    result = await registry.call(
        "get_campaign_goal_performance",
        '{"client_id":"west_export","days":120}',
        chat_id=123456789,
        request_id="r",
    )
    assert result["status"] == "ok"
    assert spy.await_count == 2  # 120 days = two Direct reports of up to 90 days
    assert any("из 2 отчётов" in text for text in result["limitations"])

    # A campaign counts only its primary goal: the frequent micro goal is not a lead.
    search = next(c for c in result["campaigns"] if c["id"] == "101")
    assert search["primary_goal"]["name"] == "Отклик на вакансию"
    micro = next(g for g in search["other_goals"] if g["id"] == "5004")
    assert Decimal(search["conversions"]) * 50 == Decimal(micro["conversions"])
    assert Decimal(search["cr"]) < 100

    # The Master campaign exposes no goal, yet it is compared on the form goal it
    # converts on, fetched from the counter, and is named rather than shown as an ID.
    goals = {g["goal_id"]: g for g in result["by_goal"]}
    assert result["by_goal"][0]["goal_id"] == "5001"
    form = goals["5003"]
    assert form["name"] == "Отправка формы"
    assert form["most_conversions"]["id"] == "102"
    assert form["most_conversions"]["goal_in_settings"] is False
    assert any("не отдал цель" in text for text in result["limitations"])

    await registry.call(
        "get_campaign_goal_performance",
        '{"client_id":"west_export","days":120}',
        chat_id=123456789,
        request_id="r2",
    )
    assert spy.await_count == 2  # chunks are cached


async def test_tool_rejects_period_over_a_year(runtime):
    result = await ToolRegistry(runtime.checks).call(
        "get_campaign_goal_performance",
        '{"client_id":"west_export","days":400}',
        chat_id=123456789,
        request_id="r",
    )
    assert result["status"] == "invalid"


async def test_gender_of_converters_is_shown_next_to_targeting(runtime, client):
    # Marina case: men were excluded by a bid adjustment, yet men leave the leads.
    client.direct.main_goal_ids, client.metrica.main_goal_ids = [], []
    result = await ToolRegistry(runtime.checks).call(
        "get_campaign_goal_performance",
        '{"client_id":"west_export","days":30,"segment":"gender","campaign_ids":["101"]}',
        chat_id=123456789,
        request_id="r",
    )
    audience = result["audience"]
    assert [c["id"] for c in result["campaigns"]] == ["101"]
    men = next(r for r in audience["total"] if r["segment"] == "GENDER_MALE")
    assert Decimal(men["conversions_share_percent"]) == Decimal("98.00")
    assert Decimal(men["clicks_share_percent"]) < 25
    assert audience["targeting_adjustments"][0]["bid_percent"] == 0


def without_goals(runtime, client):
    bare = client.model_copy(
        update={
            "direct": client.direct.model_copy(update={"main_goal_ids": []}),
            "metrica": client.metrica.model_copy(update={"main_goal_ids": []}),
        }
    )
    runtime.registry.clients[client.id] = bare
    return bare


async def test_card_takes_campaign_goals_when_none_are_chosen(runtime, client):
    from app.analytics.periods import make_period
    from app.domain.reports import CheckMode, TriggerSource

    without_goals(runtime, client)
    reports, text = await runtime.checks.run_check(
        [client.id],
        make_period("7d"),
        CheckMode.STANDARD,
        TriggerSource.INTERNAL,
        chat_id=123456789,
    )
    stored = runtime.registry.clients[client.id]
    assert stored.direct.main_goal_ids == ["5001"]  # the blind Master campaign adds none
    assert stored.direct.goals_source == "campaigns"
    assert reports[0].main_goal_ids == ["5001"]
    assert reports[0].current.conversions is not None
    assert "Цели взяты из настроек кампаний" in text
    assert "цели не выбраны" not in text.casefold()

    # A restart re-reads the stored profile with the same source.
    again = await runtime.checks.repository.configure_client(client)
    assert again.direct.goals_source == "campaigns"


async def test_manual_goals_are_never_replaced(runtime, client):
    spy = AsyncMock(wraps=runtime.checks.provider.campaign_goals)
    runtime.checks.provider.campaign_goals = spy
    assert await runtime.checks.ensure_goals(client) is client
    spy.assert_not_called()


async def test_choosing_goals_by_hand_ends_automatic_mode(runtime, client):
    bare = without_goals(runtime, client)
    auto = await runtime.checks.ensure_goals(bare)
    assert auto.direct.goals_source == "campaigns"
    manual = await runtime.checks.repository.save_client_preferences(
        auto, goal_ids=["5001", "5003"], user_id=1
    )
    assert manual.direct.goals_source == "manual"
    runtime.checks.goals_checked.clear()
    assert await runtime.checks.ensure_goals(manual) is manual


async def test_goal_tool_range_does_not_become_the_chart_period(runtime):
    # A 120-day range without comparison was stored as the dialogue period and the
    # chart after the answer crashed on AnalysisPeriod validation.
    from app.agent.service import AgentService
    from app.integrations.deepseek import LLMMessage, ToolCall

    llm = AsyncMock()
    llm.complete.side_effect = [
        LLMMessage(
            calls=[
                ToolCall(
                    "g",
                    "get_campaign_goal_performance",
                    '{"client_id":"west_export","days":120}',
                )
            ]
        ),
        LLMMessage(content="Лучшая кампания — «РСЯ — каталог»."),
    ]
    await AgentService(runtime.checks, llm).ask("Лучшая кампания за 4 месяца", 123456789, 1)
    context = await runtime.checks.repository.conversation(123456789, 1)
    assert context["active_client_id"] == "west_export"
    period = context.get("period")
    assert period is None or {"current", "previous"} <= period.keys()


async def test_campaigns_are_listed_through_v501_with_master_campaigns(client, monkeypatch):
    # Rusagro: v5 listed six text campaigns and silently left out Master campaigns.
    import json as jsonlib

    monkeypatch.setenv("DIRECT_OAUTH_TOKEN", "test-token-not-real")
    seen = []

    def handler(request):
        seen.append(request.url.path)
        body = jsonlib.loads(request.read())
        assert "UNIFIED_CAMPAIGN" in body["params"]["SelectionCriteria"]["Types"]
        return httpx.Response(
            200,
            json={
                "result": {
                    "Campaigns": [
                        {
                            "Id": 1,
                            "Name": "МК",
                            "State": "ON",
                            "Type": "UNIFIED_CAMPAIGN",
                            "UnifiedCampaign": {"PriorityGoals": {"Items": [{"GoalId": 9001}]}},
                        }
                    ]
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        goals = await DirectAdapter(ReadTransport(http)).campaign_goals(client)
    assert seen == ["/json/v501/campaigns"]
    assert goals["1"]["primary_goal_id"] == "9001"


async def test_campaign_list_falls_back_to_v5(client, monkeypatch):
    monkeypatch.setenv("DIRECT_OAUTH_TOKEN", "test-token-not-real")
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if "v501" in request.url.path:
            return httpx.Response(400, json={"error": {"error_code": 8000}})
        return httpx.Response(200, json={"result": {"Campaigns": []}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await DirectAdapter(ReadTransport(http)).campaigns(client) == []
    assert seen == ["/json/v501/campaigns", "/json/v5/campaigns"]
