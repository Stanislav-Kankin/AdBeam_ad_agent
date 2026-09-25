from datetime import date, timedelta
from typing import Literal

from pydantic import ConfigDict, Field, field_validator, model_validator

from app.analytics.periods import AnalysisPeriod, DateRange, make_period, today_moscow
from app.domain.clients import StrictModel


class ListArgs(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    offset: int = Field(default=0, ge=0, le=10000)
    top_n: int = Field(default=30, ge=1, le=50)


class ClientArgs(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    client_id: str = Field(
        min_length=1,
        max_length=200,
        description="ID, точное имя, логин или алиас клиента из list_clients.",
    )
    period: str = Field(
        default="7d",
        description="Завершённый период: yesterday или 1d–90d, например 30d или 60d.",
    )
    top_n: int = Field(default=10, ge=1, le=50)
    start_date: date | None = None
    end_date: date | None = None
    compare_start: date | None = None
    compare_end: date | None = None

    @field_validator("client_id")
    @classmethod
    def clean_client(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("Пустая ссылка на клиента.")
        return value

    @field_validator("period", mode="before")
    @classmethod
    def normalize_period(cls, value):
        if not isinstance(value, str):
            return value
        normalized = value.strip().casefold().replace(" ", "")
        aliases = {
            "day": "1d",
            "1day": "1d",
            "7days": "7d",
            "14days": "14d",
            "30days": "30d",
            "60days": "60d",
            "90days": "90d",
            "1m": "30d",
            "1month": "30d",
            "2m": "60d",
            "2months": "60d",
            "месяц": "30d",
            "2месяца": "60d",
        }
        if normalized.isdigit():
            normalized += "d"
        return aliases.get(normalized, normalized)

    @model_validator(mode="after")
    def validate_period(self):
        self.analysis_period()
        return self

    def analysis_period(self):
        dates = [self.start_date, self.end_date, self.compare_start, self.compare_end]
        if any(d is not None for d in dates):
            if not all(d is not None for d in dates):
                raise ValueError("Для произвольного сравнения укажите все четыре даты.")
            return AnalysisPeriod(
                current=DateRange(start=self.start_date, end=self.end_date),
                previous=DateRange(start=self.compare_start, end=self.compare_end),
            ).completed()
        return make_period(self.period).completed()


class MetricaReportArgs(ClientArgs):
    report: Literal["campaign", "ad", "condition", "search_phrase", "platform"] = Field(
        default="campaign",
        description=(
            "Детализация отчёта Метрики: campaign, ad, condition, search_phrase или platform."
        ),
    )
    campaign_ids: list[str] = Field(
        default_factory=list,
        max_length=100,
        description="ID, точные названия или однозначные фрагменты названий кампаний; пустой список означает все кампании клиента.",
    )
    goal_ids: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="ID целей из get_metrica_goals; пустой список использует основные цели.",
    )

    @field_validator("campaign_ids")
    @classmethod
    def campaign_references(cls, values):
        cleaned = list(dict.fromkeys(value.strip() for value in values))
        if any(not value or len(value) > 200 for value in cleaned):
            raise ValueError("Invalid campaign reference.")
        return cleaned

    @field_validator("goal_ids")
    @classmethod
    def numeric_ids(cls, values):
        cleaned = list(dict.fromkeys(value.strip() for value in values))
        if any(not value.isdigit() for value in cleaned):
            raise ValueError("Goal IDs must be numeric.")
        return cleaned


class CampaignGoalArgs(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    client_id: str = Field(
        min_length=1,
        max_length=200,
        description="ID, точное имя, логин или алиас клиента из list_clients.",
    )
    days: int = Field(
        default=30,
        ge=1,
        le=366,
        description="Последние N завершённых дней, до 366 (4 месяца = 120).",
    )
    start_date: date | None = None
    end_date: date | None = None
    top_n: int = Field(default=20, ge=1, le=50)

    @field_validator("client_id")
    @classmethod
    def clean_client(cls, value):
        return ClientArgs.clean_client(value)

    @model_validator(mode="after")
    def validate_range(self):
        self.date_range()
        return self

    def date_range(self):
        if (self.start_date is None) != (self.end_date is None):
            raise ValueError("Укажите обе даты: start_date и end_date.")
        yesterday = today_moscow() - timedelta(days=1)
        start, end = (
            (self.start_date, self.end_date)
            if self.start_date
            else (yesterday - timedelta(days=self.days - 1), yesterday)
        )
        if end > yesterday or start > end or (end - start).days + 1 > 366:
            raise ValueError("Период: завершённые дни, не больше 366.")
        return start, end
