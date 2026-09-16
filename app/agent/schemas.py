from datetime import date

from pydantic import ConfigDict, Field, model_validator

from app.analytics.periods import AnalysisPeriod, DateRange, make_period
from app.domain.clients import StrictModel


class ListArgs(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    offset: int = Field(default=0, ge=0, le=10000)
    top_n: int = Field(default=30, ge=1, le=50)


class ClientArgs(StrictModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    client_id: str = Field(pattern=r"^[a-z0-9_]{1,32}$")
    period: str = "7d"
    top_n: int = Field(default=10, ge=1, le=50)
    start_date: date | None = None
    end_date: date | None = None
    compare_start: date | None = None
    compare_end: date | None = None

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
