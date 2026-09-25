from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from app.analytics.periods import AnalysisPeriod, DateRange


class DataStatus(StrEnum):
    OK = "ok"
    EMPTY = "no_data"
    UNAVAILABLE = "unavailable"
    INSUFFICIENT = "insufficient"
    NOT_CHECKED = "not_checked"


class CheckMode(StrEnum):
    SUMMARY = "summary"
    STANDARD = "standard"
    DEEP = "deep"


class TriggerSource(StrEnum):
    SCHEDULE = "schedule"
    TELEGRAM = "telegram_command"
    AGENT = "agent"
    INTERNAL = "internal"


class Totals(BaseModel):
    spend: Decimal | None = None
    impressions: int | None = None
    clicks: int | None = None
    conversions: Decimal | None = None
    revenue: Decimal | None = None


class Metrics(Totals):
    ctr: Decimal | None = None
    cpc: Decimal | None = None
    cr: Decimal | None = None
    cpa: Decimal | None = None
    drr: Decimal | None = None


class BreakdownRow(BaseModel):
    id: str
    name: str
    totals: Totals


class DirectData(BaseModel):
    status: DataStatus
    period: DateRange
    totals: Totals = Field(default_factory=Totals)
    rows: list[BreakdownRow] = Field(default_factory=list)
    campaigns: list[dict] = Field(default_factory=list)
    campaigns_status: DataStatus = DataStatus.NOT_CHECKED
    currency: str = "RUB"
    limitations: list[str] = Field(default_factory=list)


class MetricaData(BaseModel):
    status: DataStatus
    period: DateRange
    visits: int | None = None
    users: int | None = None
    pageviews: int | None = None
    bounce_rate: Decimal | None = None
    page_depth: Decimal | None = None
    avg_visit_duration_seconds: Decimal | None = None
    goals: list[dict] = Field(default_factory=list)
    missing_goal_ids: list[str] = Field(default_factory=list)
    sampled: bool = False
    scope: Literal["counter", "direct_campaigns"] = "counter"
    timezone: str = "Europe/Moscow"
    limitations: list[str] = Field(default_factory=list)


class RevenueData(BaseModel):
    status: DataStatus
    period: DateRange
    source: str
    amount: Decimal | None = None
    comparable: bool = False
    reason: str = ""


class Snapshot(BaseModel):
    direct: DirectData
    metrica: MetricaData
    revenue: RevenueData


class Signal(BaseModel):
    type: str
    level: Literal["green", "yellow", "red"]
    message: str
    actual: dict
    period: AnalysisPeriod
    evidence: str
    confidence: Literal["low", "medium", "high"]
    sufficient_data: bool
    next_check: str


class ClientReport(BaseModel):
    client_id: str
    client_name: str
    period: AnalysisPeriod
    mode: CheckMode
    status: str
    level: str
    current: Metrics
    previous: Metrics
    changes: dict
    signals: list[Signal] = Field(default_factory=list)
    drivers: list[dict] = Field(default_factory=list)
    checks: dict[str, str] = Field(default_factory=dict)
    source_status: dict[str, str] = Field(default_factory=dict)
    limitations: list[str] = Field(default_factory=list)
    main_goal_ids: list[str] = Field(default_factory=list)
    goals_source: Literal["manual", "campaigns"] = "manual"
    targets: dict = Field(default_factory=dict)
    goal_scope: Literal["counter", "direct_campaigns"] = "counter"
    goal_metrics: list[dict] = Field(default_factory=list)
    metrica_current: dict = Field(default_factory=dict)
    metrica_previous: dict = Field(default_factory=dict)
    mock: bool = False
    generated_at: datetime
