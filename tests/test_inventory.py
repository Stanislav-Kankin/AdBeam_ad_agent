import json

import httpx

from app.integrations.http import ReadTransport
from app.integrations.inventory import MetricaInventory


async def test_inventory_persists_counter_and_goal_selection(runtime, client, monkeypatch):
    monkeypatch.setenv("DIRECT_OAUTH_TOKEN", "synthetic")
    monkeypatch.setenv("METRICA_OAUTH_TOKEN", "synthetic")
    client.metrica.counter_id = None
    client.metrica.counter_ids = []

    def respond(request):
        if request.url.host == "api.direct.yandex.com":
            payload = json.loads(request.content)
            assert payload["method"] == "get"
            return httpx.Response(
                200,
                json={
                    "result": {"Campaigns": [{"TextCampaign": {"CounterIds": {"Items": [5, 6]}}}]}
                },
            )
        if request.url.path == "/management/v1/counters":
            assert request.url.params["offset"] == "1"
            return httpx.Response(
                200,
                json={
                    "rows": 2,
                    "counters": [
                        {"id": 5, "name": "Main", "site": "main.test", "permission": "edit"},
                        {"id": 7, "name": "Other", "site": "other.test", "permission": "view"},
                    ],
                },
            )
        if request.url.path.endswith("/counter/5/goals"):
            return httpx.Response(
                200,
                json={"goals": [{"id": 42, "name": "Lead", "type": "action"}]},
            )
        raise AssertionError(request.url)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        service = MetricaInventory(ReadTransport(http), runtime.checks.repository, runtime.registry)
        counters = await service.refresh_client(client, refresh=True)
        assert [(row["id"], row["status"]) for row in counters] == [
            (5, "ok"),
            (6, "forbidden"),
        ]
        configured = await service.select_counters(client, [5], user_id=1)
        await service.goals(configured, 5)
        configured = await service.select_goals(configured, ["42"], user_id=1)

    assert configured.metrica.selected_counter_ids() == [5]
    assert configured.direct.main_goal_ids == ["42"]
    assert configured.metrica.main_goal_ids == ["42"]
    restored = await runtime.checks.repository.configure_client(client)
    assert restored.metrica.selected_counter_ids() == [5]
    assert restored.direct.main_goal_ids == ["42"]


async def test_inventory_can_select_unlinked_accessible_counter(runtime, client, monkeypatch):
    monkeypatch.setenv("DIRECT_OAUTH_TOKEN", "synthetic")
    monkeypatch.setenv("METRICA_OAUTH_TOKEN", "synthetic")
    client.metrica.counter_id = None
    client.metrica.counter_ids = []

    def respond(request):
        if request.url.host == "api.direct.yandex.com":
            return httpx.Response(200, json={"result": {"Campaigns": []}})
        return httpx.Response(
            200,
            json={
                "rows": 1,
                "counters": [
                    {"id": 7, "name": "Manual", "site": "manual.test", "permission": "view"}
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        service = MetricaInventory(ReadTransport(http), runtime.checks.repository, runtime.registry)
        await service.refresh_client(client, refresh=True)
        configured = await service.select_counters(client, [7], user_id=1)
    assert configured.metrica.selected_counter_ids() == [7]
