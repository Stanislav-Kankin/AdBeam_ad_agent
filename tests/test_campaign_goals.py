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
    assert {goal["goal_id"] for goal in result["by_goal"]} == {"5001", "5002"}
    for goal in result["by_goal"]:
        assert goal["best"]["id"] == ("101" if goal["goal_id"] == "5001" else "102")
    assert any("из 2 отчётов" in text for text in result["limitations"])

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
