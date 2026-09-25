from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PositiveMoney = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
GoalId = Annotated[str, Field(pattern=r"^\d+$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DirectConfig(StrictModel):
    client_login: str = Field(min_length=1, pattern=r"^[a-zA-Z0-9_.@-]+$")
    main_goal_ids: list[GoalId] = Field(default_factory=list, max_length=10)
    # manual: chosen by a person (YAML or the menu); campaigns: taken automatically
    # from the goals set inside the campaigns while nobody chose any.
    goals_source: Literal["manual", "campaigns"] = "manual"
    token_env: str = Field(
        default="DIRECT_OAUTH_TOKEN",
        pattern=r"^(?:DIRECT|ADBEAM)_[A-Z0-9_]*(?:TOKEN|API_KEY)$",
        max_length=100,
    )
    attribution_model: Literal["AUTO", "LC", "FCCD", "LSCCD"] = "LC"
    timezone: Literal["Europe/Moscow"] = "Europe/Moscow"


class MetricaConfig(StrictModel):
    counter_id: int | None = Field(default=None, gt=0)
    counter_ids: list[int] = Field(default_factory=list, max_length=20)
    main_goal_ids: list[GoalId] = Field(default_factory=list, max_length=10)
    token_env: str = Field(
        default="METRICA_OAUTH_TOKEN",
        pattern=r"^(?:METRICA|ADBEAM)_[A-Z0-9_]*(?:TOKEN|API_KEY)$",
        max_length=100,
    )

    @model_validator(mode="after")
    def unique_counters(self):
        values = [*(self.counter_ids or []), *([self.counter_id] if self.counter_id else [])]
        if len(values) != len(set(values)):
            raise ValueError("Duplicate counter IDs")
        return self

    def selected_counter_ids(self) -> list[int]:
        return [*(self.counter_ids or []), *([self.counter_id] if self.counter_id else [])]


class Targets(StrictModel):
    # Main project KPI. None means CPA when primary goals are configured, else spend.
    kpi: Literal["cpa", "drr", "conversions"] | None = None
    target_cpa: PositiveMoney | None = None
    target_drr: PositiveMoney | None = None
    monthly_budget: PositiveMoney | None = None
    weekly_budget: PositiveMoney | None = None
    minimum_spend_for_analysis: PositiveMoney = Decimal("3000")
    conversion_delay_days: int = Field(default=3, ge=0, le=90)
    minimum_clicks: int = Field(default=100, ge=1)
    minimum_conversions: int = Field(default=5, ge=1)
    no_conversion_cpa_multiple: float = Field(default=2, gt=0)
    cpa_excess_percent: float = Field(default=30, gt=0)
    kpi_change_tolerance_percent: float = Field(default=3, ge=0, le=100)
    cpc_change_percent: float = Field(default=30, gt=0)
    cr_drop_percent: float = Field(default=25, gt=0, le=100)
    spend_change_percent: float = Field(default=25, gt=0)
    budget_deviation_percent: float = Field(default=25, gt=0)


class RoistatFilter(StrictModel):
    field: str = Field(pattern=r"^marker_level_[1-7]$")
    operator: Literal["=", "in"] = "="
    value: str | list[str]


class RevenueConfig(StrictModel):
    source: Literal["none", "metrica_ecommerce", "roistat"] = "none"
    roistat_project_id: int | None = Field(default=None, gt=0)
    token_env: str = Field(
        default="ROISTAT_API_KEY",
        pattern=r"^(?:ROISTAT|ADBEAM)_[A-Z0-9_]*(?:TOKEN|API_KEY)$",
        max_length=100,
    )
    roistat_filters: list[RoistatFilter] = Field(default_factory=list)
    currency: Literal["RUB"] = "RUB"
    attribution_confirmed: bool = False

    @model_validator(mode="after")
    def validate_roistat(self):
        if self.source == "roistat" and not self.roistat_project_id:
            raise ValueError("roistat_project_id required")
        return self


class TelegramConfig(StrictModel):
    allowed_chat_ids: list[int] = Field(default_factory=list)


class Client(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9_]{1,32}$")
    name: str = Field(min_length=1, max_length=100)
    aliases: list[str] = Field(default_factory=list)
    active: bool = True
    direct: DirectConfig
    metrica: MetricaConfig
    targets: Targets = Field(default_factory=Targets)
    revenue: RevenueConfig = Field(default_factory=RevenueConfig)
    telegram: TelegramConfig
    mock_scenario: Literal["green", "yellow", "red", "unavailable"] = "green"

    @model_validator(mode="after")
    def same_goals(self):
        if set(self.direct.main_goal_ids) != set(self.metrica.main_goal_ids):
            raise ValueError("Direct and Metrica primary goals must match")
        for goals in (self.direct.main_goal_ids, self.metrica.main_goal_ids):
            if len(goals) != len(set(goals)):
                raise ValueError("Duplicate goal IDs")
        return self
