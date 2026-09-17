"""Persistent inventory of counters and goals available to an agency client."""

import asyncio
from time import monotonic

from app.config import secret_from_env
from app.integrations.discovery import campaign_counters
from app.integrations.http import IntegrationError

BASE_URL = "https://api-metrika.yandex.net"


class MetricaInventory:
    def __init__(self, transport, repository, registry):
        self.transport = transport
        self.repository = repository
        self.registry = registry
        self.lock = asyncio.Lock()
        self._accessible = None
        self._accessible_at = 0.0

    @staticmethod
    def headers(client):
        token = secret_from_env(client.metrica.token_env)
        if not token:
            raise IntegrationError("metrica", "missing_token")
        return {"Authorization": f"OAuth {token}"}

    async def accessible_counters(self, client, *, refresh=False):
        if not refresh and self._accessible is not None and monotonic() - self._accessible_at < 300:
            return self._accessible
        async with self.lock:
            if (
                not refresh
                and self._accessible is not None
                and monotonic() - self._accessible_at < 300
            ):
                return self._accessible
            rows, offset = [], 1
            while True:
                data = await self.transport.json(
                    "metrica",
                    "GET",
                    BASE_URL + "/management/v1/counters",
                    headers=self.headers(client),
                    params={"per_page": 100, "offset": offset},
                )
                counters = data.get("counters")
                if not isinstance(counters, list):
                    raise IntegrationError("metrica", "invalid_counters_response")
                rows.extend(
                    {
                        "id": int(row["id"]),
                        "name": str(row.get("name") or "")[:200],
                        "site": str(row.get("site") or "")[:300],
                        "permission": str(row.get("permission") or "")[:30],
                        "status": "ok",
                    }
                    for row in counters
                )
                offset += len(counters)
                total = int(data.get("rows", len(rows)))
                if not counters or offset > total:
                    break
            await self.repository.save_counter_catalog(rows)
            self._accessible, self._accessible_at = rows, monotonic()
            return rows

    async def goals(self, client, counter_id):
        data = await self.transport.json(
            "metrica",
            "GET",
            f"{BASE_URL}/management/v1/counter/{int(counter_id)}/goals",
            headers=self.headers(client),
        )
        goals = data.get("goals")
        if not isinstance(goals, list):
            raise IntegrationError("metrica", "invalid_goals_response")
        cleaned = [
            {
                "id": str(row["id"]),
                "name": str(row.get("name") or row["id"])[:200],
                "type": str(row.get("type") or "")[:50],
            }
            for row in goals
        ]
        existing = next(
            (
                row
                for row in await self.repository.client_counters(client.id, include_all=True)
                if row["id"] == int(counter_id)
            ),
            {"id": int(counter_id), "status": "ok"},
        )
        await self.repository.save_counter_catalog([{**existing, "goals": cleaned}])
        return cleaned

    async def refresh_client(self, client, *, refresh=False):
        accessible, linked = await asyncio.gather(
            self.accessible_counters(client, refresh=refresh),
            campaign_counters(self.transport, client),
        )
        by_id = {row["id"]: row for row in accessible}
        selected = set(client.metrica.selected_counter_ids())
        relevant = set(linked) | selected
        catalog = []
        for counter_id in sorted(relevant):
            row = by_id.get(counter_id)
            if row:
                catalog.append(
                    {
                        **row,
                        "linked": counter_id in linked,
                        "selected": counter_id in selected,
                    }
                )
            else:
                catalog.append(
                    {
                        "id": counter_id,
                        "name": f"Счётчик {counter_id}",
                        "site": "",
                        "permission": "",
                        "status": "forbidden",
                        "linked": counter_id in linked,
                        "selected": counter_id in selected,
                    }
                )
        await self.repository.save_counter_catalog(catalog)
        await self.repository.save_client_counters(client.id, catalog)
        return await self.repository.client_counters(client.id)

    async def select_counters(self, client, counter_ids, user_id):
        catalog = await self.repository.client_counters(client.id, include_all=True)
        available = {row["id"] for row in catalog if row["status"] == "ok"}
        before = {row["id"] for row in catalog if row["selected"]}
        selected = list(dict.fromkeys(int(value) for value in counter_ids))
        if any(value not in available for value in selected):
            raise PermissionError("Счётчик недоступен текущему токену Метрики.")
        updated = await self.repository.save_client_preferences(
            client, counter_ids=selected, user_id=user_id
        )
        await self.repository.save_client_counters(
            client.id,
            [
                {**row, "selected": row["id"] in selected}
                for row in catalog
                if row["linked"] or row["id"] in selected
            ],
        )
        if before - set(selected):
            remaining_goals = {
                goal["id"]
                for row in catalog
                if row["id"] in selected
                for goal in row["goals"]
            }
            goals = [goal for goal in updated.metrica.main_goal_ids if goal in remaining_goals]
            updated = await self.repository.save_client_preferences(
                updated, goal_ids=goals, user_id=user_id
            )
        self.registry.clients[client.id] = updated
        return updated

    async def select_goals(self, client, goal_ids, user_id):
        available = {
            goal["id"]
            for row in await self.repository.client_counters(client.id)
            if row["selected"] and row["status"] == "ok"
            for goal in row["goals"]
        }
        selected = list(dict.fromkeys(str(value) for value in goal_ids))
        if len(selected) > 10:
            raise ValueError("В отчёт Директа можно выбрать не более 10 основных целей.")
        if any(value not in available for value in selected):
            raise PermissionError("Цель не относится к выбранным доступным счётчикам.")
        updated = await self.repository.save_client_preferences(
            client, goal_ids=selected, user_id=user_id
        )
        self.registry.clients[client.id] = updated
        return updated
