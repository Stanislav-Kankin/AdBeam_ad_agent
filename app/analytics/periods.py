import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, model_validator

MOSCOW = ZoneInfo("Europe/Moscow")


def today_moscow() -> date:
    return datetime.now(MOSCOW).date()


class DateRange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    start: date
    end: date

    @model_validator(mode="after")
    def ordered(self):
        if not 1 <= self.days <= 90:
            raise ValueError("Период должен содержать от 1 до 90 дней.")
        return self

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def completed(self, today: date | None = None):
        if self.end >= (today or today_moscow()):
            raise ValueError("Разрешены только завершённые дни по Москве.")
        return self

    def label(self) -> str:
        return f"{self.start:%d.%m.%Y}–{self.end:%d.%m.%Y}"


class AnalysisPeriod(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    current: DateRange
    previous: DateRange

    @model_validator(mode="after")
    def comparable(self):
        if self.previous.end >= self.current.start:
            raise ValueError("Сравниваемые периоды не должны пересекаться.")
        if self.previous.days != self.current.days:
            raise ValueError("Сравниваемые периоды должны иметь одинаковую длину.")
        return self

    def completed(self, today: date | None = None):
        self.current.completed(today)
        self.previous.completed(today)
        return self


def make_period(value: str = "7d", today: date | None = None) -> AnalysisPeriod:
    today = today or today_moscow()
    if value in ("yesterday", "вчера"):
        days = 1
    elif re.fullmatch(r"[1-9]\d?d", value):
        days = int(value[:-1])
    else:
        raise ValueError("Период: yesterday или 1d–90d, например 7d.")
    end = today - timedelta(days=1)
    start = end - timedelta(days=days - 1)
    shift = timedelta(days=7 if days == 1 else days)
    return AnalysisPeriod(
        current=DateRange(start=start, end=end),
        previous=DateRange(start=start - shift, end=end - shift),
    )
