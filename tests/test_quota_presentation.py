import asyncio

import httpx
import pytest

from app.analytics.periods import make_period
from app.bot.markdown import markdown_parts
from app.domain.reports import CheckMode, ClientReport
from app.integrations.http import IntegrationError, ReadTransport
from app.reporting.formatter import detailed


async def test_quota_stops_retries_but_keeps_management_available():
    calls = []

    async def handler(request):
        calls.append(request.url.path)
        return httpx.Response(429 if "/stat/" in request.url.path else 200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        transport = ReadTransport(http)
        for _ in range(3):
            with pytest.raises(IntegrationError) as exc:
                await transport.json("metrica", "GET", "https://example.org/stat/v1/data")
            assert exc.value.code == "quota_cooldown_429"
        await transport.json("metrica", "GET", "https://example.org/management/v1/counters")
    assert len(calls) == 2


async def test_cache_isolated_by_credentials_and_returns_copies():
    calls = []

    async def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"totals": [7]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        transport = ReadTransport(http)
        transport.metrica_interval = 0
        url = "https://example.org/stat/v1/data"
        first = await transport.json("metrica", "GET", url, headers={"Authorization": "A"})
        first["totals"][0] = 99
        assert (await transport.json("metrica", "GET", url, headers={"Authorization": "A"}))[
            "totals"
        ] == [7]
        await transport.json("metrica", "GET", url, headers={"Authorization": "B"})
    assert len(calls) == 2


async def test_metrica_parallel_limit_and_pacing(monkeypatch):
    now, waits, running, maximum = 100.0, [], 0, 0

    async def sleep(delay):
        nonlocal now
        waits.append(delay)
        now += delay
        await asyncio.sleep(0)

    async def handler(request):
        nonlocal running, maximum
        running += 1
        maximum = max(maximum, running)
        await asyncio.sleep(0)
        running -= 1
        return httpx.Response(200, json={})

    monkeypatch.setattr("app.integrations.http.monotonic", lambda: now)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        transport = ReadTransport(http, sleep=sleep)
        await asyncio.gather(
            *(
                transport.request("metrica", "GET", "https://example.org/stat/v1/data")
                for _ in range(6)
            )
        )
    assert maximum <= 2
    assert sum(waits) >= 8.4


def test_markdown_unicode_entity_splitting():
    parts = list(
        markdown_parts(
            "# Отчёт\n**😀Показатель 1234567890**\n`ab-grandline`\n<b>текст</b>", limit=14
        )
    )
    text = "".join(part for part, _ in parts)
    assert text == "Отчёт\n😀Показатель 1234567890\nab-grandline\n<b>текст</b>"
    assert any(e.type == "bold" for _, entities in parts for e in entities)
    for part, entities in parts:
        size = len(part.encode("utf-16-le")) // 2
        assert size <= 14
        assert all(0 <= e.offset < e.offset + e.length <= size for e in entities)


async def test_report_delta_survives_json_and_explains_direction(runtime, client):
    report = await runtime.checks.analyze(client, make_period(), CheckMode.STANDARD)
    report = ClientReport.model_validate_json(report.model_dump_json())
    report.changes["ctr"] = {"absolute": "1", "percent": "25"}
    text = detailed(report)
    assert "сейчас " in text and "; раньше " in text
    assert "изменение +1 п.п.; относительно прошлого периода +25%" in text
    assert "←" not in text
