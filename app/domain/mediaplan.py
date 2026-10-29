"""Media plan of a client read from a Google Sheet: the monthly Yandex Direct budget
and leads the agency promised, which reports compare with the facts."""

import re
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.clients import GoalId

SHEET_URL = re.compile(
    r"^https://docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]{20,100})(?:[/?#].*)?$"
)
GID = re.compile(r"[#?&]gid=(\d{1,12})")
MONTH = re.compile(r"^20\d\d-(0[1-9]|1[0-2])$")

Amount = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


def sheet_location(url):
    """Spreadsheet id and sheet gid from a link; ValueError for anything else."""
    url = url.strip()
    match = SHEET_URL.match(url)
    if not match:
        raise ValueError("Нужна ссылка вида https://docs.google.com/spreadsheets/d/…")
    gid = GID.search(url)
    return match[1], gid[1] if gid else None


class PlanMonth(BaseModel):
    model_config = ConfigDict(extra="forbid")
    month: str
    budget: Amount | None = None
    leads: Amount | None = None
    clicks: Amount | None = None
    # Agency reward by KPI completion step: {"50": 13787, …, "100": 55147}.
    rewards: dict[str, Amount] = Field(default_factory=dict, max_length=20)

    @field_validator("month")
    @classmethod
    def month_format(cls, value):
        if not MONTH.match(value):
            raise ValueError("month must be YYYY-MM")
        return value

    @field_validator("rewards")
    @classmethod
    def reward_steps(cls, value):
        for step in value:
            if not step.isdigit() or not 0 < int(step) <= 300:
                raise ValueError("reward steps are percents")
        return value

    @property
    def cpl(self):
        if self.budget and self.leads:
            return self.budget / self.leads
        return None


class MediaPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    months: list[PlanMonth] = Field(min_length=1, max_length=36)
    # The fact from Direct is without VAT; a plan with VAT is recalculated.
    budget_vat: Literal["without", "with", "unknown"] = "unknown"
    lead_definition: str = Field(default="", max_length=600)
    goal_ids: list[GoalId] = Field(default_factory=list, max_length=40)
    # "If the budget exceeds the plan, the planned leads grow in proportion."
    leads_scale_with_budget: bool = False
    notes: list[Annotated[str, Field(max_length=300)]] = Field(default_factory=list, max_length=5)

    @field_validator("months")
    @classmethod
    def unique_months(cls, value):
        if len({item.month for item in value}) != len(value):
            raise ValueError("duplicate months")
        return sorted(value, key=lambda item: item.month)

    def month(self, key):
        return next((item for item in self.months if item.month == key), None)
