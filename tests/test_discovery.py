import json

import httpx
import pytest

from app.analytics.periods import make_period
from app.config import ClientRegistry, load_clients
from app.integrations.discovery import AccountDiscovery, campaign_counters
from app.integrations.http import IntegrationError, ReadTransport
from app.integrations.metrica import MetricaAdapter


async def test_account_pagination_acl_and_removal(settings, monkeypatch):
    monkeypatch.setenv("DIRECT_OAUTH_TOKEN", "synthetic")
    settings.telegram_admin_user_ids = [123456789]
    registry = ClientRegistry([], settings.telegram_allowed_chat_ids)
    pages = []

    def respond(request):
        assert "Client-Login" not in request.headers
        params = json.loads(request.content)["params"]
        assert params["SelectionCriteria"] == {"Archived": "NO"}
        offset = params["Page"]["Offset"]
        pages.append(offset)
        return httpx.Response(
            200,
            json={
                "result": {
                    "Clients": [
                        {"Login": f"client-{offset}", "ClientInfo": "Company", "Currency": "RUB"}
                    ],
                    **({"LimitedBy": 1} if offset == 0 else {}),
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        service = AccountDiscovery(ReadTransport(http), registry, settings)
        assert await service.refresh() == 2
        assert pages == [0, 1]
        assert len(registry.visible(123456789)) == 2
        assert registry.visible(123456789)[0].name == "Company · client-0"
        assert registry.resolve(123456789, "client-0")[0].direct.client_login == "client-0"
        assert not registry.visible(999)
        ids = list(registry.clients)
        await service.refresh()
        assert list(registry.clients) == ids

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(403))
    ) as http:
        service = AccountDiscovery(ReadTransport(http), registry, settings)
        with pytest.raises(IntegrationError):
            await service.refresh()
        assert not registry.clients


def test_production_ignores_demo_yaml(settings):
    settings.app_mode = "production"
    assert not load_clients(settings).clients


async def test_counter_discovery_paginates_and_deduplicates(client, monkeypatch):
    monkeypatch.setenv("DIRECT_OAUTH_TOKEN", "synthetic")

    def respond(request):
        assert request.headers["Client-Login"] == client.direct.client_login
        offset = json.loads(request.content)["params"]["Page"]["Offset"]
        return httpx.Response(
            200,
            json={
                "result": {
                    "Campaigns": [{"UnifiedCampaign": {"CounterIds": {"Items": [5, 6]}}}],
                    **({"LimitedBy": 1} if offset == 0 else {}),
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        assert await campaign_counters(ReadTransport(http), client) == [5, 6]


async def test_metrica_batches_goals_and_campaigns(client, monkeypatch):
    monkeypatch.setenv("METRICA_OAUTH_TOKEN", "synthetic")
    period = make_period("7d").current
    calls = []

    def respond(request):
        metrics = request.url.params["metrics"].split(",")
        assert len(metrics) <= 20
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "query": {"date1": str(period.start), "date2": str(period.end)},
                "totals": [1] * len(metrics),
                "sampled": False,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result = await MetricaAdapter(ReadTransport(http)).report(
            client,
            period,
            [str(i) for i in range(201)],
            [f"ym:s:goal{i}reaches" for i in range(45)],
        )
    assert len(calls) == 9
    assert result["totals"] == [3] * 45


async def test_no_counter_returns_explicit_limitation(client, monkeypatch):
    monkeypatch.setenv("DIRECT_OAUTH_TOKEN", "synthetic")
    client.metrica.counter_id = None
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"result": {"Campaigns": []}})
        )
    ) as http:
        result = await MetricaAdapter(ReadTransport(http)).overview(
            client, make_period("7d").current, ["1"]
        )
    assert result.status == "not_checked"
    assert result.limitations


async def test_auto_counters_keep_all_goals_separate(client, monkeypatch):
    monkeypatch.setenv("DIRECT_OAUTH_TOKEN", "synthetic")
    monkeypatch.setenv("METRICA_OAUTH_TOKEN", "synthetic")
    client.metrica.counter_id = None
    period = make_period("7d").current

    def respond(request):
        if request.url.host == "api.direct.yandex.com":
            return httpx.Response(
                200,
                json={
                    "result": {"Campaigns": [{"TextCampaign": {"CounterIds": {"Items": [5, 6]}}}]}
                },
            )
        if request.url.path.endswith("/goals"):
            return httpx.Response(
                200, json={"goals": [{"id": 42, "name": "Purchase", "type": "action"}]}
            )
        if "/management/" in request.url.path:
            return httpx.Response(200, json={"counter": {"time_zone_name": "Europe/Moscow"}})
        return httpx.Response(
            200,
            json={
                "query": {"date1": str(period.start), "date2": str(period.end)},
                "totals": [100, 90, 200, 20, 2, 60, 7],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result = await MetricaAdapter(ReadTransport(http)).overview(client, period, ["1"])
    assert result.visits is None
    assert result.status == "insufficient"
    assert [(g["counter_id"], g["id"], g["reaches"]) for g in result.goals] == [
        (5, "42", "7"),
        (6, "42", "7"),
    ]


async def test_goal_catalog_does_not_multiply_reports_by_campaign_chunks(client, monkeypatch):
    monkeypatch.setenv("METRICA_OAUTH_TOKEN", "synthetic")
    client.metrica.counter_id = 5
    period = make_period("7d").current
    stat_calls = []

    def respond(request):
        if request.url.path.endswith("/goals"):
            return httpx.Response(
                200,
                json={
                    "goals": [
                        {"id": i, "name": f"Goal {i}", "type": "action"} for i in range(1, 123)
                    ]
                },
            )
        if "/management/" in request.url.path:
            return httpx.Response(200, json={"counter": {"time_zone_name": "Europe/Moscow"}})
        stat_calls.append(request)
        assert "filters" not in request.url.params
        metrics = request.url.params["metrics"].split(",")
        return httpx.Response(
            200,
            json={
                "query": {"date1": str(period.start), "date2": str(period.end)},
                "totals": [1] * len(metrics),
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result = await MetricaAdapter(ReadTransport(http))._overview(
            client,
            period,
            [str(i) for i in range(2169)],
            all_goals=True,
        )
    assert result.scope == "counter"
    assert len(stat_calls) == 7
    assert len(result.goals) == 122
