from app.config import secret_from_env
from app.domain.reports import DataStatus, RevenueData
from app.integrations.http import IntegrationError, ReadTransport, number

URL = "https://cloud.roistat.com/api/v1/project/analytics/data"


class RoistatAdapter:
    def __init__(self, transport: ReadTransport):
        self.transport = transport

    async def revenue(self, client, period):
        config = client.revenue
        token = secret_from_env(config.token_env)
        if not token:
            raise IntegrationError("roistat", "missing_token")
        if not config.roistat_filters or not config.attribution_confirmed:
            raise IntegrationError("roistat", "channel_scope_or_attribution_not_configured")
        data = await self.transport.json(
            "roistat",
            "POST",
            URL,
            headers={"Api-key": token},
            params={"project": config.roistat_project_id},
            json={
                "dimensions": [],
                "metrics": ["revenue"],
                "period": {
                    "from": f"{period.start}T00:00:00+0300",
                    "to": f"{period.end}T23:59:59+0300",
                },
                "filters": [
                    {"field": f.field, "operation": f.operator, "value": f.value}
                    for f in config.roistat_filters
                ],
            },
        )
        try:
            # mean is the overall total; don't also sum interval/row totals.
            if data["status"] != "success" or len(data["data"]) != 1:
                raise ValueError
            amount = number(data["data"][0]["mean"]["metrics"]["revenue"]["value"])
        except (KeyError, TypeError, ValueError, IndexError):
            raise IntegrationError("roistat", "invalid_aggregate_response") from None
        return RevenueData(
            status=DataStatus.OK,
            period=period,
            source="roistat",
            amount=amount,
            comparable=True,
            reason="Канал, RUB и атрибуция подтверждены в конфиге проекта.",
        )
