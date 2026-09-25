import asyncio
import csv
import hashlib
import io
import json
import logging
from decimal import Decimal

from app.analytics.metrics import aggregate
from app.config import secret_from_env
from app.domain.reports import BreakdownRow, DataStatus, DirectData, Totals
from app.integrations.http import IntegrationError, ReadTransport, number
from app.security import redact

REPORTS_URL = "https://api.direct.yandex.com/json/v5/reports"
CAMPAIGNS_URL = "https://api.direct.yandex.com/json/v5/campaigns"
# Unified performance campaigns (what the Master of campaigns creates) are listed by
# API version 5.01 only; v5 silently leaves them out although Reports count them.
CAMPAIGNS_URL_V501 = "https://api.direct.yandex.com/json/v501/campaigns"
ALL_CAMPAIGN_TYPES = [
    "TEXT_CAMPAIGN",
    "UNIFIED_CAMPAIGN",
    "DYNAMIC_TEXT_CAMPAIGN",
    "SMART_CAMPAIGN",
    "MOBILE_APP_CAMPAIGN",
    "CPM_BANNER_CAMPAIGN",
]
BID_MODIFIERS_URL = "https://api.direct.yandex.com/json/v5/bidmodifiers"
SEGMENT_FIELDS = {"gender": "Gender", "age": "Age", "income": "IncomeGrade"}
DIMENSIONS = {
    "date": ("ACCOUNT_PERFORMANCE_REPORT", ["Date"]),
    "campaign": ("CAMPAIGN_PERFORMANCE_REPORT", ["CampaignId", "CampaignName"]),
    "device": ("CUSTOM_REPORT", ["Device"]),
    "geo": ("CUSTOM_REPORT", ["LocationOfPresenceId"]),
    "search": ("SEARCH_QUERY_PERFORMANCE_REPORT", ["Query"]),
    "placement": ("CUSTOM_REPORT", ["Placement"]),
    "age": ("CUSTOM_REPORT", ["Age"]),
    "gender": ("CUSTOM_REPORT", ["Gender"]),
    "income": ("CUSTOM_REPORT", ["IncomeGrade"]),
}
REPORT_PAGE_SIZE = 10000
REPORT_MAX_PAGES = 100
REPORT_GOALS_LIMIT = 10  # Direct Reports accepts at most ten goals per report
GOAL_CAMPAIGN_TYPES = ("TextCampaign", "UnifiedCampaign", "DynamicTextCampaign", "SmartCampaign")

logger = logging.getLogger(__name__)


async def get_campaigns(transport, headers, params):
    """Campaigns.get through v501 with every campaign type, falling back to v5 (the
    previous behaviour) if v501 rejects the request."""
    v501 = {
        **params,
        "SelectionCriteria": {**params["SelectionCriteria"], "Types": ALL_CAMPAIGN_TYPES},
    }
    try:
        return await transport.json(
            "direct",
            "POST",
            CAMPAIGNS_URL_V501,
            headers=headers,
            json={"method": "get", "params": v501},
        )
    except IntegrationError as exc:
        logger.warning("Campaigns v501 failed, using v5 error=%s", exc)
        return await transport.json(
            "direct",
            "POST",
            CAMPAIGNS_URL,
            headers=headers,
            json={"method": "get", "params": params},
        )


def strategy_goals(value):
    """Goal IDs a bidding strategy optimises for, wherever the strategy nests them.
    Small IDs are Direct placeholders (for example 13 = "key goals"), not Metrica goals."""
    found = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "GoalId" and child is not None and int(child) > 1000:
                found.append(str(child))
            else:
                found.extend(strategy_goals(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(strategy_goals(child))
    return found


def parse_goal_tsv(text: str, goals: list[str], attribution: str, segment: str | None = None):
    """Campaign rows with conversions kept per goal instead of summed; with a
    segment field (Gender, Age, IncomeGrade) the key is "<campaign>|<value>"."""
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")), delimiter="\t")
    required = {"CampaignId", "CampaignName", "Cost", "Impressions", "Clicks"}
    if segment:
        required.add(segment)
    columns = {goal: f"Conversions_{goal}_{attribution}" for goal in goals}
    if not reader.fieldnames or not required.issubset(reader.fieldnames):
        raise IntegrationError("direct", "invalid_tsv_columns")
    if not set(columns.values()).issubset(reader.fieldnames):
        raise IntegrationError("direct", "missing_goal_columns")
    rows = {}
    for raw in reader:
        campaign_id = str(raw["CampaignId"])
        value = str(raw.get(segment) or "UNKNOWN") if segment else None
        rows[f"{campaign_id}|{value}" if segment else campaign_id] = {
            "campaign_id": campaign_id,
            "segment": value,
            "name": redact(str(raw.get("CampaignName") or ""))[:200],
            "spend": number(raw.get("Cost")),
            "impressions": int(number(raw.get("Impressions"))),
            "clicks": int(number(raw.get("Clicks"))),
            "goals": {
                goal: Decimal(0) if raw.get(column) in ("--", "") else number(raw.get(column))
                for goal, column in columns.items()
            },
        }
    return rows


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
        # Direct Reports uses `--` for a goal with no attributed conversions
        # (including in Yandex's official Metrica report example). It is zero,
        # while a missing/empty column means that the metric is unavailable.
        conversion_total = (
            sum((Decimal(0) if v == "--" else number(v) for v in conversions), Decimal(0))
            if conversions and all(v not in (None, "") for v in conversions)
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
        # Direct queues offline reports per advertiser. A slot is held while a report is
        # pending, so a shared global limit let one slow account block every other client.
        self.report_locks = {}

    def report_lock(self, client):
        login = client.direct.client_login
        if login not in self.report_locks:
            self.report_locks[login] = asyncio.Semaphore(3)
        return self.report_locks[login]

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

    def report_params(self, client, period, dimension):
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
        return params, fields

    async def breakdown_page(self, client, period, dimension, page):
        params, fields = self.report_params(client, period, dimension)
        page_params = {
            **params,
            "Page": {"Limit": REPORT_PAGE_SIZE, "Offset": page * REPORT_PAGE_SIZE},
        }
        page_params["ReportName"] = (
            "adbeam_"
            + hashlib.sha256(json.dumps(page_params, sort_keys=True).encode()).hexdigest()[:24]
        )
        logger.info(
            "Direct report page client=%s dimension=%s page=%s",
            client.id,
            dimension,
            page + 1,
        )
        async with self.report_lock(client):
            response = await self.transport.request(
                "direct",
                "POST",
                REPORTS_URL,
                pending=True,
                headers=self.headers(client),
                json={"params": page_params},
            )
        rows = parse_tsv(
            response.text,
            client.direct.main_goal_ids,
            client.direct.attribution_model,
            fields,
        )
        return rows, len(rows) < REPORT_PAGE_SIZE

    async def breakdown(self, client, period, dimension="campaign", *, max_pages=REPORT_MAX_PAGES):
        rows, limited = [], False
        for page in range(max_pages):
            batch, complete = await self.breakdown_page(client, period, dimension, page)
            rows.extend(batch)
            if complete:
                break
        else:
            limited = True
        return DirectData(
            status=DataStatus.INSUFFICIENT
            if limited
            else DataStatus.OK
            if rows
            else DataStatus.EMPTY,
            period=period,
            rows=rows,
            totals=aggregate([r.totals for r in rows]),
            limitations=[
                f"Разрез «{dimension}» ограничен первыми "
                f"{max_pages * REPORT_PAGE_SIZE:,} строками с наибольшим расходом; "
                "итоги разреза неполные."
            ]
            if limited
            else [],
        )

    async def campaigns(self, client):
        rows, offset = [], 0
        for _ in range(20):
            data = await get_campaigns(
                self.transport,
                self.headers(client),
                {
                    "SelectionCriteria": {},
                    "FieldNames": ["Id", "Name", "State", "Status", "StatusPayment", "Currency"],
                    "Page": {"Limit": 1000, "Offset": offset},
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

    async def campaign_goals(self, client):
        """Goals set inside each campaign: key goals (PriorityGoals) and the strategy goal."""
        try:
            return await self.campaign_goals_of(client, GOAL_CAMPAIGN_TYPES)
        except IntegrationError as exc:
            # A field unsupported for one campaign type fails the whole request;
            # text and unified campaigns cover most accounts.
            logger.warning("Campaign goals retry client=%s error=%s", client.id, exc)
            return await self.campaign_goals_of(client, ("TextCampaign", "UnifiedCampaign"))

    async def campaign_goals_of(self, client, kinds):
        campaigns, offset = {}, 0
        for _ in range(20):
            data = await get_campaigns(
                self.transport,
                self.headers(client),
                {
                    "SelectionCriteria": {},
                    "FieldNames": ["Id", "Name", "State", "Type"],
                    **{
                        f"{kind}FieldNames": [
                            "PriorityGoals",
                            "BiddingStrategy",
                            # Smart campaigns name it CounterId and are skipped here.
                            *(["CounterIds"] if kind != "SmartCampaign" else []),
                        ]
                        for kind in kinds
                    },
                    "Page": {"Limit": 1000, "Offset": offset},
                },
            )
            result = data.get("result", {})
            if not isinstance(result.get("Campaigns"), list):
                raise IntegrationError("direct", "invalid_campaign_response")
            for row in result["Campaigns"]:
                settings = next(
                    (row[kind] for kind in kinds if isinstance(row.get(kind), dict)),
                    {},
                )
                items = [
                    item
                    for item in (settings.get("PriorityGoals") or {}).get("Items") or []
                    if item.get("GoalId") and int(item["GoalId"]) > 1000
                ]
                priority = [str(item["GoalId"]) for item in items]
                strategy = strategy_goals(settings.get("BiddingStrategy"))
                # The goal the campaign is judged by: the one its strategy optimises,
                # otherwise the most valuable key goal. Other key goals are secondary;
                # summing them would count micro-conversions as leads.
                valued = sorted(items, key=lambda item: -(item.get("Value") or 0))
                primary = strategy[0] if strategy else str(valued[0]["GoalId"]) if valued else None
                # Display (CPM) campaigns pay for impressions and have no goals by design.
                if not primary and row.get("Type") != "CPM_BANNER_CAMPAIGN":
                    bidding = settings.get("BiddingStrategy") or {}
                    logger.info(
                        "Campaign without goals client=%s id=%s type=%s settings=%s "
                        "search=%s network=%s",
                        client.id,
                        row.get("Id"),
                        row.get("Type"),
                        sorted(settings),
                        (bidding.get("Search") or {}).get("BiddingStrategyType"),
                        (bidding.get("Network") or {}).get("BiddingStrategyType"),
                    )
                campaigns[str(row["Id"])] = {
                    "name": redact(str(row.get("Name") or ""))[:200],
                    "state": str(row.get("State") or ""),
                    "type": str(row.get("Type") or ""),
                    "primary_goal_id": primary,
                    "priority_goal_ids": priority,
                    "strategy_goal_ids": strategy,
                    "goal_ids": list(dict.fromkeys(priority + strategy)),
                    "counter_ids": [
                        str(value)
                        for value in (settings.get("CounterIds") or {}).get("Items") or []
                    ],
                }
            if "LimitedBy" not in result:
                return campaigns
            offset = int(result["LimitedBy"])
        raise IntegrationError("direct", "campaign_limit")

    async def goal_report(self, client, period, goal_ids, segment=None):
        """Campaign spend and conversions per goal; goals are requested in batches of ten.
        segment (gender, age, income) splits every campaign row by that audience field."""
        field = SEGMENT_FIELDS.get(segment) if segment else None
        merged = {}
        for index in range(0, len(goal_ids), REPORT_GOALS_LIMIT):
            batch = goal_ids[index : index + REPORT_GOALS_LIMIT]
            params = {
                "SelectionCriteria": {"DateFrom": str(period.start), "DateTo": str(period.end)},
                "FieldNames": [
                    "CampaignId",
                    "CampaignName",
                    *([field] if field else []),
                    "Impressions",
                    "Clicks",
                    "Cost",
                    "Conversions",
                ],
                "ReportType": "CUSTOM_REPORT" if field else "CAMPAIGN_PERFORMANCE_REPORT",
                "DateRangeType": "CUSTOM_DATE",
                "Format": "TSV",
                "IncludeVAT": "NO",
                "IncludeDiscount": "NO",
                "Goals": batch,
                "AttributionModels": [client.direct.attribution_model],
            }
            params["ReportName"] = (
                "adbeam_goals_"
                + hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:24]
            )
            async with self.report_lock(client):
                response = await self.transport.request(
                    "direct",
                    "POST",
                    REPORTS_URL,
                    pending=True,
                    headers=self.headers(client),
                    json={"params": params},
                )
            for key, row in parse_goal_tsv(
                response.text, batch, client.direct.attribution_model, field
            ).items():
                target = merged.setdefault(key, {**row, "goals": {}})
                target["goals"].update(row["goals"])
        return merged

    async def query(
        self, client, start, end, *, report_type, fields, filters, goals, order_by, limit
    ):
        """Arbitrary read-only Reports API query built by the agent (validated upstream).
        Returns raw columns; with goals, Conversions etc. come per goal."""
        params = {
            "SelectionCriteria": {"DateFrom": str(start), "DateTo": str(end)},
            "FieldNames": fields,
            "ReportType": report_type,
            "DateRangeType": "CUSTOM_DATE",
            "Format": "TSV",
            "IncludeVAT": "NO",
            "IncludeDiscount": "NO",
            "Page": {"Limit": limit},
        }
        if filters:
            params["SelectionCriteria"]["Filter"] = filters
        if goals:
            params["Goals"] = goals
            params["AttributionModels"] = [client.direct.attribution_model]
        if order_by:
            params["OrderBy"] = order_by
        params["ReportName"] = (
            "adbeam_q_"
            + hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:24]
        )
        async with self.report_lock(client):
            response = await self.transport.request(
                "direct",
                "POST",
                REPORTS_URL,
                pending=True,
                headers=self.headers(client),
                json={"params": params},
            )
        reader = csv.DictReader(io.StringIO(response.text.lstrip("﻿")), delimiter="\t")
        rows = [
            {
                key: redact(value)[:300] if isinstance(value, str) else value
                for key, value in raw.items()
            }
            for raw in reader
        ]
        return {"columns": list(reader.fieldnames or []), "rows": rows}

    async def demographic_adjustments(self, client, campaign_ids):
        """Gender/age bid adjustments per campaign: what the targeting was set to."""
        found = []
        for index in range(0, len(campaign_ids), 10):
            offset = 0
            for _ in range(20):
                data = await self.transport.json(
                    "direct",
                    "POST",
                    BID_MODIFIERS_URL,
                    headers=self.headers(client),
                    json={
                        "method": "get",
                        "params": {
                            "SelectionCriteria": {
                                "CampaignIds": [int(v) for v in campaign_ids[index : index + 10]],
                                "Types": ["DEMOGRAPHICS_ADJUSTMENT"],
                                "Levels": ["CAMPAIGN", "AD_GROUP"],
                            },
                            "FieldNames": ["CampaignId", "AdGroupId", "Level"],
                            "DemographicsAdjustmentFieldNames": [
                                "Gender",
                                "Age",
                                "BidModifier",
                                "Enabled",
                            ],
                            "Page": {"Limit": 1000, "Offset": offset},
                        },
                    },
                )
                result = data.get("result", {})
                for row in result.get("BidModifiers") or []:
                    item = row.get("DemographicsAdjustment") or {}
                    if item.get("Enabled") == "NO":
                        continue
                    found.append(
                        {
                            "campaign_id": str(row.get("CampaignId")),
                            "level": str(row.get("Level") or ""),
                            "gender": item.get("Gender"),
                            "age": item.get("Age"),
                            # 100 = no change, 0 = the segment is excluded (-100%).
                            "bid_percent": item.get("BidModifier"),
                        }
                    )
                if "LimitedBy" not in result:
                    break
                offset = int(result["LimitedBy"])
        return found

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
