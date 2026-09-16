import asyncio
import csv
import hashlib
import io
import json
from decimal import Decimal

from app.analytics.metrics import aggregate
from app.config import secret_from_env
from app.domain.reports import BreakdownRow, DataStatus, DirectData, Totals
from app.integrations.http import IntegrationError, ReadTransport, number
from app.security import redact

REPORTS_URL = "https://api.direct.yandex.com/json/v5/reports"
CAMPAIGNS_URL = "https://api.direct.yandex.com/json/v5/campaigns"
DIMENSIONS = {
    "campaign": ("CAMPAIGN_PERFORMANCE_REPORT", ["CampaignId", "CampaignName"]),
    "device": ("CUSTOM_REPORT", ["Device"]),
    "geo": ("CUSTOM_REPORT", ["LocationOfPresenceId"]),
    "search": ("SEARCH_QUERY_PERFORMANCE_REPORT", ["Query"]),
    "placement": ("CUSTOM_REPORT", ["Placement"]),
}


def parse_tsv(text: str, goals: list[str], attribution: str, fields: list[str]):
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")), delimiter="\t")
    required = {*fields, "Cost", "Impressions", "Clicks"}
    if not reader.fieldnames or not required.issubset(reader.fieldnames):
        raise IntegrationError("direct", "invalid_tsv_columns")
    columns = [f"Conversions_{goal}_{attribution}" for goal in goals]
    if not set(columns).issubset(reader.fieldnames):
        raise IntegrationError("direct", "missing_goal_columns")
    rows = []
    for raw in reader:
        conversions = [raw.get(column) for column in columns]
        conversion_total = (
            sum((number(v) for v in conversions), Decimal(0))
            if conversions and all(v not in (None, "", "--") for v in conversions)
            else None
        )
        totals = Totals(
            spend=number(raw.get("Cost")),
            impressions=int(number(raw.get("Impressions"))),
            clicks=int(number(raw.get("Clicks"))),
            conversions=conversion_total,
        )
        name = redact(str(raw.get(fields[-1], "Не определено")))[:200]
        rows.append(
            BreakdownRow(id=str(raw.get(fields[0], "unknown"))[:200], name=name, totals=totals)
        )
    return rows


class DirectAdapter:
    def __init__(self, transport: ReadTransport):
        self.transport = transport
        # One scheduled client uses two periods; leave capacity for an interactive report.
        self.report_lock = asyncio.Semaphore(3)

    def headers(self, client):
        token = secret_from_env(client.direct.token_env)
        if not token:
            raise IntegrationError("direct", "missing_token")
        return {
            "Authorization": f"Bearer {token}",
            "Client-Login": client.direct.client_login,
            "Accept-Language": "en",
            "processingMode": "auto",
            "returnMoneyInMicros": "false",
            "skipReportHeader": "true",
            "skipReportSummary": "true",
            "skipColumnHeader": "false",
        }

    async def breakdown(self, client, period, dimension="campaign"):
        report_type, fields = DIMENSIONS[dimension]
        params = {
            "SelectionCriteria": {"DateFrom": str(period.start), "DateTo": str(period.end)},
            "FieldNames": [*fields, "Impressions", "Clicks", "Cost"],
            "ReportType": report_type,
            "DateRangeType": "CUSTOM_DATE",
            "Format": "TSV",
            "IncludeVAT": "NO",
            "IncludeDiscount": "NO",
            "OrderBy": [{"Field": "Cost", "SortOrder": "DESCENDING"}],
            "Page": {"Limit": 10000},
        }
        if client.direct.main_goal_ids:
            params.update(
                Goals=client.direct.main_goal_ids,
                AttributionModels=[client.direct.attribution_model],
            )
            params["FieldNames"].append("Conversions")
        if dimension == "placement":
            params["SelectionCriteria"]["Filter"] = [
                {"Field": "AdNetworkType", "Operator": "EQUALS", "Values": ["AD_NETWORK"]}
            ]
        params["ReportName"] = (
            "adbeam_" + hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:24]
        )
        async with self.report_lock:
            response = await self.transport.request(
                "direct",
                "POST",
                REPORTS_URL,
                pending=True,
                headers=self.headers(client),
                json={"params": params},
            )
        rows = parse_tsv(
            response.text, client.direct.main_goal_ids, client.direct.attribution_model, fields
        )
        limited = len(rows) >= 10000
        return DirectData(
            status=DataStatus.INSUFFICIENT
            if limited
            else DataStatus.OK
            if rows
            else DataStatus.EMPTY,
            period=period,
            rows=rows,
            totals=aggregate([r.totals for r in rows]),
            limitations=["Достигнут лимит 10000 строк; итоги неполные."] if limited else [],
        )

    async def campaigns(self, client):
        rows, offset = [], 0
        for _ in range(20):
            data = await self.transport.json(
                "direct",
                "POST",
                CAMPAIGNS_URL,
                headers=self.headers(client),
                json={
                    "method": "get",
                    "params": {
                        "SelectionCriteria": {},
                        "FieldNames": [
                            "Id",
                            "Name",
                            "State",
                            "Status",
                            "StatusPayment",
                            "Currency",
                        ],
                        "Page": {"Limit": 1000, "Offset": offset},
                    },
                },
            )
            result = data.get("result", {})
            if not isinstance(result.get("Campaigns"), list):
                raise IntegrationError("direct", "invalid_campaign_response")
            rows.extend(
                {
                    k: redact(v) if isinstance(v, str) else v
                    for k, v in row.items()
                    if k in {"Id", "Name", "State", "Status", "StatusPayment", "Currency"}
                }
                for row in result["Campaigns"]
            )
            if "LimitedBy" not in result:
                return rows
            offset = int(result["LimitedBy"])
        raise IntegrationError("direct", "campaign_limit")

    async def overview(self, client, period):
        report, campaigns = await asyncio.gather(
            self.breakdown(client, period), self.campaigns(client), return_exceptions=True
        )
        if isinstance(report, Exception):
            report = DirectData(
                status=DataStatus.UNAVAILABLE,
                period=period,
                limitations=[
                    "direct: "
                    + (report.code if isinstance(report, IntegrationError) else "invalid_response")
                ],
            )
        if isinstance(campaigns, Exception):
            report.campaigns_status = DataStatus.UNAVAILABLE
            report.limitations.append("Статусы кампаний недоступны.")
        else:
            report.campaigns, report.campaigns_status = campaigns, DataStatus.OK
            if any(c.get("Currency") != "RUB" for c in campaigns):
                report.status, report.totals = DataStatus.INSUFFICIENT, Totals()
                report.limitations.append(
                    "Валюта кампаний не RUB; денежные показатели несопоставимы."
                )
        return report
