import json
from decimal import Decimal

import httpx
import pytest

from app.analytics.periods import make_period
from app.domain.clients import RevenueConfig
from app.domain.reports import DataStatus
from app.integrations.deepseek import DeepSeekProvider
from app.integrations.direct import DirectAdapter, parse_tsv
from app.integrations.http import IntegrationError, ReadTransport
from app.integrations.metrica import MetricaAdapter
from app.integrations.roistat import RoistatAdapter

TSV = "CampaignId\tCampaignName\tImpressions\tClicks\tCost\tConversions_123456_LC\n101\tSearch\t1000\t100\t123.45\t2\n"


@pytest.mark.parametrize(
    "bad",
    [
        "{not tsv}",
        "Cost\tClicks\n1\t2\n",
        TSV.replace("123.45", "NaN"),
        TSV.replace("123.45", "-10"),
    ],
)
def test_invalid_tsv_rejected(bad):
    with pytest.raises(IntegrationError):
        parse_tsv(bad, ["123456"], "LC", ["CampaignId", "CampaignName"])


def test_tsv_goals_micros_empty_and_unknown():
    rows = parse_tsv(TSV, ["123456"], "LC", ["CampaignId", "CampaignName"])
    assert rows[0].totals.spend == Decimal("123.45")
    assert rows[0].totals.conversions == 2
    rows = parse_tsv(
        TSV.replace("\t2\n", "\t--\n"), ["123456"], "LC", ["CampaignId", "CampaignName"]
    )
    assert rows[0].totals.conversions == 0
    assert (
        parse_tsv(TSV.splitlines()[0] + "\n", ["123456"], "LC", ["CampaignId", "CampaignName"])
        == []
    )
    two_goals = TSV.replace(
        "Conversions_123456_LC", "Conversions_123456_LC\tConversions_789_LC"
    ).replace("\t2\n", "\t2\t--\n")
    assert (
        parse_tsv(two_goals, ["123456", "789"], "LC", ["CampaignId", "CampaignName"])[
            0
        ].totals.conversions
        == 2
    )


async def test_direct_pending_retries_identical_read_request(client, monkeypatch):
    monkeypatch.setenv("DIRECT_OAUTH_TOKEN", "test-token-not-real")
    requests, delays = [], []
    codes = iter([201, 202, 429, 200])

    async def handler(request):
        requests.append(request)
        return httpx.Response(next(codes), headers={"retryIn": "3"}, text=TSV)

    async def sleep(delay):
        delays.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await DirectAdapter(ReadTransport(http, sleep=sleep)).breakdown(
            client, make_period().current
        )
    assert result.status == DataStatus.OK
    assert delays == [3, 3, 4]
    assert len({r.content for r in requests}) == 1
    assert all(r.headers["Client-Login"] == client.direct.client_login for r in requests)
    assert requests[0].headers["returnMoneyInMicros"] == "false"
    assert json.loads(requests[0].content)["params"]["Goals"] == ["123456"]


async def test_direct_report_paginates_until_short_page(client, monkeypatch):
    monkeypatch.setenv("DIRECT_OAUTH_TOKEN", "test-token-not-real")
    monkeypatch.setattr("app.integrations.direct.REPORT_PAGE_SIZE", 2)
    offsets = []

    def handler(request):
        params = json.loads(request.content)["params"]
        offset = params["Page"]["Offset"]
        offsets.append(offset)
        header = TSV.splitlines()[0]
        rows = {
            0: ["101\tOne\t10\t2\t3\t1", "102\tTwo\t20\t4\t6\t2"],
            2: ["103\tThree\t30\t6\t9\t3"],
        }[offset]
        return httpx.Response(200, text=header + "\n" + "\n".join(rows) + "\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await DirectAdapter(ReadTransport(http)).breakdown(client, make_period().current)
    assert offsets == [0, 2]
    assert len(result.rows) == 3
    assert result.totals.spend == 18
    assert not result.limitations


async def test_direct_report_page_cap_returns_top_slice(client, monkeypatch):
    monkeypatch.setenv("DIRECT_OAUTH_TOKEN", "test-token-not-real")
    monkeypatch.setattr("app.integrations.direct.REPORT_PAGE_SIZE", 2)
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        header = TSV.splitlines()[0]
        rows = ["101\tOne\t10\t2\t3\t1", "102\tTwo\t20\t4\t6\t2"]
        return httpx.Response(200, text=header + "\n" + "\n".join(rows) + "\n")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await DirectAdapter(ReadTransport(http)).breakdown(
            client, make_period().current, max_pages=1
        )
    assert calls == 1
    assert result.status == DataStatus.INSUFFICIENT
    assert "2 строками" in result.limitations[0]


async def test_http_does_not_retry_auth_or_expose_response():
    count = 0

    async def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(403, text="secret response with personal information")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        with pytest.raises(IntegrationError, match="http_403") as error:
            await ReadTransport(http).request("direct", "POST", "https://example.invalid")
    assert count == 1 and "secret" not in str(error.value)


async def test_huge_retry_after_does_not_violate_rate_limit():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(429, headers={"Retry-After": "3600"})
        )
    ) as http:
        with pytest.raises(IntegrationError, match="retry_later"):
            await ReadTransport(http).request("direct", "POST", "https://example.invalid")


async def test_direct_campaign_pagination_read_only(client, monkeypatch):
    monkeypatch.setenv("DIRECT_OAUTH_TOKEN", "test-token")
    offsets = []

    async def handler(request):
        payload = json.loads(request.content)
        assert payload["method"] == "get"
        offset = payload["params"]["Page"]["Offset"]
        offsets.append(offset)
        return httpx.Response(
            200,
            json={
                "result": {
                    "Campaigns": [{"Id": offset + 1, "Name": "One", "Currency": "RUB"}],
                    **({"LimitedBy": 1} if offset == 0 else {}),
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await DirectAdapter(ReadTransport(http)).campaigns(client)
    assert offsets == [0, 1] and len(result) == 2


def metrica_handler(period, *, sampled=False, timezone="Europe/Moscow", missing=False):
    def handle(request):
        assert request.method == "GET"
        if request.url.path.endswith("/goals"):
            return httpx.Response(
                200, json={"goals": [] if missing else [{"id": 123456, "name": "Purchase"}]}
            )
        if request.url.path.startswith("/management/"):
            return httpx.Response(200, json={"counter": {"time_zone_name": timezone}})
        query = request.url.params
        assert "filters" not in query
        assert query["accuracy"] == "full"
        metrics = query["metrics"].split(",")
        values = [100, 90, 200, 20, 2, 60, 5]
        return httpx.Response(
            200,
            json={
                "query": {"date1": str(period.start), "date2": str(period.end)},
                "totals": values[: len(metrics)],
                "sampled": sampled,
            },
        )

    return handle


@pytest.mark.parametrize(
    "sampled,missing,status",
    [
        (False, False, DataStatus.OK),
        (True, False, DataStatus.INSUFFICIENT),
        (False, True, DataStatus.INSUFFICIENT),
    ],
)
async def test_metrica_counter_goals_sampling(client, monkeypatch, sampled, missing, status):
    monkeypatch.setenv("METRICA_OAUTH_TOKEN", "test-token")
    period = make_period().current
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(metrica_handler(period, sampled=sampled, missing=missing))
    ) as http:
        result = await MetricaAdapter(ReadTransport(http)).overview(client, period, [101, 102])
    assert result.status == status
    assert result.scope == "counter"
    assert result.visits == 100
    assert result.missing_goal_ids == (["123456"] if missing else [])


async def test_metrica_rejects_different_timezone_and_unscoped_data(client, monkeypatch):
    monkeypatch.setenv("METRICA_OAUTH_TOKEN", "test-token")
    period = make_period().current
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(metrica_handler(period, timezone="Asia/Tokyo"))
    ) as http:
        adapter = MetricaAdapter(ReadTransport(http))
        with pytest.raises(IntegrationError, match="timezone"):
            await adapter.overview(client, period, [101, 102])
        with pytest.raises(IntegrationError, match="scope"):
            await adapter.report(client, period, [], ["ym:s:visits"])


async def test_metrica_rejects_wrong_dates_and_missing_totals(client, monkeypatch):
    monkeypatch.setenv("METRICA_OAUTH_TOKEN", "test-token")
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, json={"query": {"date1": "2000-01-01", "date2": "2000-01-01"}, "totals": [500]}
            )
        )
    ) as http:
        with pytest.raises(IntegrationError, match="period_mismatch"):
            await MetricaAdapter(ReadTransport(http)).report(
                client, make_period().current, [101], ["ym:s:visits"]
            )


async def test_metrica_direct_report_builds_campaign_goal_and_behavior_rows(client, monkeypatch):
    monkeypatch.setenv("METRICA_OAUTH_TOKEN", "test-token")
    period = make_period().current
    requests = []

    def handler(request):
        if request.url.path.endswith("/goals"):
            return httpx.Response(200, json={"goals": [{"id": 123456, "name": "Purchase"}]})
        query = request.url.params
        requests.append(query)
        metrics = query["metrics"].split(",")
        values = {
            "ym:s:visits": 100,
            "ym:s:users": 80,
            "ym:s:bounceRate": 12.5,
            "ym:s:pageDepth": 4.2,
            "ym:s:avgVisitDurationSeconds": 180,
            "ym:s:goal123456visits": 7,
            "ym:s:goal123456conversionRate": 7,
        }
        return httpx.Response(
            200,
            json={
                "query": {"date1": str(period.start), "date2": str(period.end)},
                "data": [
                    {
                        "dimensions": [{"id": "101", "name": "Search"}],
                        "metrics": [values[name] for name in metrics],
                    }
                ],
                "total_rows": 1,
                "sampled": False,
                "sample_share": 1,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await MetricaAdapter(ReadTransport(http)).direct_report(
            client,
            period,
            "campaign",
            goal_ids=["123456"],
            campaign_ids=["101"],
        )

    assert len(requests) == 2
    assert requests[0]["dimensions"] == "ym:s:lastDirectClickOrder"
    assert requests[0]["filters"] == "ym:s:lastDirectClickOrder=.(101)"
    assert result["rows"][0]["metrics"]["bounce_rate"] == Decimal("12.5")
    assert result["rows"][0]["metrics"]["goal_123456_visits"] == 7
    assert result["goals"] == [{"id": "123456", "name": "Purchase"}]


async def test_roistat_aggregates_only_and_does_not_double_count(client, monkeypatch):
    monkeypatch.setenv("ROISTAT_API_KEY", "test-token")
    client.revenue = RevenueConfig(
        source="roistat",
        roistat_project_id=123,
        attribution_confirmed=True,
        roistat_filters=[{"field": "marker_level_1", "operator": "=", "value": "yandex"}],
    )

    async def handler(request):
        assert request.url.path == "/api/v1/project/analytics/data"
        payload = json.loads(request.content)
        assert payload["metrics"] == ["revenue"] and payload["dimensions"] == []
        assert payload["filters"][0]["operation"] == "="
        assert request.headers["Api-key"] == "test-token"
        return httpx.Response(
            200,
            json={
                "status": "success",
                "data": [
                    {
                        "items": [{"metrics": {"revenue": {"value": 500}}}],
                        "mean": {"metrics": {"revenue": {"value": 500}}},
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        adapter = RoistatAdapter(ReadTransport(http))
        revenue = await adapter.revenue(client, make_period().current)
        assert revenue.amount == 500
        client.revenue.roistat_filters = []
        with pytest.raises(IntegrationError, match="scope"):
            await adapter.revenue(client, make_period().current)


async def test_deepseek_sdk_request_and_tool_call_parsing(settings):
    from openai import AsyncOpenAI

    settings.deepseek_api_key = "synthetic-key"
    # Construct using SecretStr because assignment validation is deliberately not enabled.
    from pydantic import SecretStr

    settings.deepseek_api_key = SecretStr("synthetic-key")
    provider = DeepSeekProvider(settings)
    await provider.client.close()

    async def handler(request):
        body = json.loads(request.content)
        assert request.url.path == "/chat/completions"
        assert body["model"] == "deepseek-v4-flash"
        assert body["thinking"] == {"type": "disabled"}
        assert "synthetic-key" not in request.content.decode()
        return httpx.Response(
            200,
            json={
                "id": "test",
                "object": "chat.completion",
                "created": 1,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "one",
                                    "type": "function",
                                    "function": {"name": "list_clients", "arguments": "{}"},
                                }
                            ],
                        },
                    }
                ],
            },
        )

    provider.client = AsyncOpenAI(
        api_key="synthetic-key",
        base_url="https://api.deepseek.com",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    try:
        from app.agent.tools import tool_schemas

        result = await provider.complete(
            [{"role": "user", "content": "List clients"}], tool_schemas()
        )
        assert result.calls[0].name == "list_clients"
        assert result.as_dict()["tool_calls"][0]["id"] == "one"
    finally:
        await provider.close()
