"""Read the agency's actual client list; never import demo configuration."""

import asyncio
import hashlib

from app.config import secret_from_env
from app.domain.clients import Client, DirectConfig, MetricaConfig, TelegramConfig
from app.integrations.http import IntegrationError


class AccountDiscovery:
    def __init__(self, transport, registry, settings):
        self.transport, self.registry, self.settings = transport, registry, settings
        self.lock = asyncio.Lock()

    async def refresh(self):
        async with self.lock:
            token = secret_from_env("DIRECT_OAUTH_TOKEN")
            if not token:
                self.registry.clients = {}
                raise IntegrationError("direct", "missing_token")
            # New account clients are only exposed in explicitly selected chats,
            # or personal admin chats already present in the global allowlist.
            chats = self.settings.yandex_client_chat_ids or [
                uid
                for uid in self.settings.telegram_admin_user_ids
                if uid in self.settings.telegram_allowed_chat_ids
            ]
            chats = [c for c in chats if c in self.settings.telegram_allowed_chat_ids]
            found, offset = {}, 0
            try:
                while True:
                    data = await self.transport.json(
                        "direct",
                        "POST",
                        "https://api.direct.yandex.com/json/v5/agencyclients",
                        headers={"Authorization": f"Bearer {token}", "Accept-Language": "ru"},
                        json={
                            "method": "get",
                            "params": {
                                "SelectionCriteria": {"Archived": "NO"},
                                "FieldNames": ["Login", "ClientInfo", "ClientId", "Currency"],
                                "Page": {"Limit": 1000, "Offset": offset},
                            },
                        },
                    )
                    result = data["result"]
                    for raw in result["Clients"]:
                        login = raw["Login"]
                        label = str(raw.get("ClientInfo") or "").strip()
                        name = (
                            f"{label[:55]} · {login}"[:100] if label and label != login else login
                        )
                        cid = "yd_" + hashlib.sha256(login.encode()).hexdigest()[:24]
                        found[cid] = Client(
                            id=cid,
                            name=name,
                            aliases=[login, label] if label else [login],
                            direct=DirectConfig(client_login=login),
                            metrica=MetricaConfig(),
                            telegram=TelegramConfig(allowed_chat_ids=chats),
                        )
                    next_offset = result.get("LimitedBy")
                    if next_offset is None:
                        break
                    if not isinstance(next_offset, int) or next_offset <= offset:
                        raise IntegrationError("direct", "invalid_pagination")
                    offset = next_offset
            except Exception:
                # Fail closed: an old list must not imply continued account access.
                self.registry.clients = {}
                raise
            self.registry.clients = dict(sorted(found.items(), key=lambda item: item[1].name))
            return len(found)


async def campaign_counters(transport, client):
    ids, offset = set(), 0
    token = secret_from_env(client.direct.token_env)
    if not token:
        raise IntegrationError("direct", "missing_token")
    while True:
        data = await transport.json(
            "direct",
            "POST",
            "https://api.direct.yandex.com/json/v5/campaigns",
            headers={
                "Authorization": f"Bearer {token}",
                "Client-Login": client.direct.client_login,
            },
            json={
                "method": "get",
                "params": {
                    "SelectionCriteria": {},
                    "FieldNames": ["Id"],
                    "TextCampaignFieldNames": ["CounterIds"],
                    "UnifiedCampaignFieldNames": ["CounterIds"],
                    "CpmBannerCampaignFieldNames": ["CounterIds"],
                    "Page": {"Limit": 1000, "Offset": offset},
                },
            },
        )
        result = data["result"]
        for campaign in result["Campaigns"]:
            for name in ("TextCampaign", "UnifiedCampaign", "CpmBannerCampaign"):
                counters = (campaign.get(name) or {}).get("CounterIds") or {}
                ids.update(counters.get("Items", []))
        next_offset = result.get("LimitedBy")
        if next_offset is None:
            return sorted(ids)
        if not isinstance(next_offset, int) or next_offset <= offset:
            raise IntegrationError("direct", "invalid_pagination")
        offset = next_offset
