import json
from decimal import Decimal

import httpx
import pytest

from app.agent.schemas import DirectQueryArgs, MetricaQueryArgs
from app.agent.tools import ToolRegistry
from app.integrations.http import IntegrationError, ReadTransport


async def call(runtime, name, args):
    return await ToolRegistry(runtime.checks).call(
        name, json.dumps(args, ensure_ascii=False), chat_id=123456789, request_id="r"
    )


async def test_catalog_lists_counters_and_named_goals(runtime):
    result = await call(runtime, "get_metrica_catalog", {"client_id": "west_export"})
    goals = {g["name"] for g in result["counters"][0]["goals"]}
    assert result["status"] == "ok" and "Отправка формы" in goals


async def test_agent_builds_gender_by_leads_report_with_comparison(runtime):
    # Marina: "targeting was women, yet 98% of leads are men" is one Metrica query.
    result = await call(
        runtime,
        "query_metrica",
        {
            "client_id": "west_export",
            "days": 30,
            "metrics": ["ym:s:visits", "ym:s:goal5003reaches"],
            "dimensions": ["ym:s:gender"],
            "filters": "ym:s:<attribution>DirectClickOrder=.('102')",
            "compare": True,
        },
    )
    assert result["status"] == "ok"
    men = next(r for r in result["rows"] if r["dimensions"][0]["id"] == "GENDER_MALE")
    assert Decimal(men["metrics"]["ym:s:goal5003reaches"]) > 0
    assert "percent" in men["changes"]["ym:s:visits"]
    assert result["compare_period"]


async def test_foreign_counter_is_never_queried(runtime):
    result = await call(
        runtime,
        "query_metrica",
        {"client_id": "west_export", "counter_id": 999999, "metrics": ["ym:s:visits"]},
    )
    assert result["status"] == "invalid" and "не относится к клиенту" in result["error"]


@pytest.mark.parametrize(
    "bad",
    [["ym:s:clientID"], ["ym:s:ipAddress"], ["ym:s:paramsLevel1"], ["DROP TABLE"], []],
)
def test_personal_and_malformed_metrica_fields_are_rejected(bad):
    with pytest.raises(ValueError):
        MetricaQueryArgs.model_validate(
            {"client_id": "c", "metrics": ["ym:s:visits"], "dimensions": bad}
            if bad
            else {"client_id": "c", "metrics": bad}
        )


def test_direct_query_args_build_api_filters():
    args = DirectQueryArgs.model_validate(
        {
            "client_id": "c",
            "report_type": "SEARCH_QUERY_PERFORMANCE_REPORT",
            "fields": ["Query", "Clicks", "Cost", "Conversions"],
            "filters": [{"field": "Cost", "operator": "GREATER_THAN", "values": ["1000"]}],
            "goal_ids": ["5003"],
            "order_by": [{"field": "Cost"}],
        }
    )
    assert args.filters[0].api() == {
        "Field": "Cost",
        "Operator": "GREATER_THAN",
        "Values": ["1000"],
    }
    assert args.order_by[0].api() == {"Field": "Cost", "SortOrder": "DESCENDING"}


async def test_direct_query_returns_rows_and_changes(runtime):
    result = await call(
        runtime,
        "query_direct",
        {
            "client_id": "west_export",
            "report_type": "CAMPAIGN_PERFORMANCE_REPORT",
            "fields": ["CampaignName", "Clicks", "Cost"],
            "days": 14,
            "compare": True,
        },
    )
    assert result["status"] == "ok" and result["rows"]
    assert "Cost" in result["rows"][0]["changes"]
    assert "CampaignName" not in result["rows"][0]["changes"]


async def test_api_rejection_reason_reaches_the_model(runtime, monkeypatch):
    async def reject(*args, **kwargs):
        raise IntegrationError(
            "metrica", "http_400_invalid_parameter", "Wrong parameter: 'metrics', value: ym:s:foo"
        )

    monkeypatch.setattr(runtime.checks.provider, "metrica_query", reject)
    result = await call(
        runtime, "query_metrica", {"client_id": "west_export", "metrics": ["ym:s:foo"]}
    )
    assert result["status"] == "invalid" and "ym:s:foo" in result["error"]


async def test_metrica_400_message_is_kept_and_bounded(client, monkeypatch):
    monkeypatch.setenv("METRICA_OAUTH_TOKEN", "test-token-not-real")

    def handler(request):
        return httpx.Response(
            400, json={"errors": [{"error_type": "invalid_parameter", "message": "Wrong x" * 100}]}
        )

    from app.integrations.metrica import MetricaAdapter

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(IntegrationError) as caught:
            await MetricaAdapter(ReadTransport(http)).query(
                client,
                1,
                "2026-09-01",
                "2026-09-02",
                metrics=["ym:s:x"],
                dimensions=[],
                filters=None,
                sort=None,
                limit=5,
            )
    assert caught.value.detail.startswith("Wrong x") and len(caught.value.detail) <= 300
