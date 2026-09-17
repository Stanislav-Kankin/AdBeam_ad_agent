from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import JSON, Boolean, DateTime, Float, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now():
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Run(Base):
    __tablename__ = "check_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    app_mode: Mapped[str] = mapped_column(String(20), index=True)
    chat_id: Mapped[str] = mapped_column(String(30), index=True)
    user_id: Mapped[str | None] = mapped_column(String(30), index=True)
    trigger: Mapped[str] = mapped_column(String(30))
    mode: Mapped[str] = mapped_column(String(20))
    period: Mapped[dict] = mapped_column(JSON)
    client_ids: Mapped[list] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(20), default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    reports: Mapped[list] = mapped_column(JSON, default=list)
    errors: Mapped[list] = mapped_column(JSON, default=list)
    report_text: Mapped[str] = mapped_column(Text, default="")


class ToolEvent(Base):
    __tablename__ = "tool_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    request_id: Mapped[str] = mapped_column(String(36), index=True)
    app_mode: Mapped[str] = mapped_column(String(20))
    chat_id: Mapped[str] = mapped_column(String(30))
    user_id: Mapped[str | None] = mapped_column(String(30), index=True)
    tool: Mapped[str] = mapped_column(String(80))
    client_id: Mapped[str | None] = mapped_column(String(32))
    arguments: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30))
    error: Mapped[str | None] = mapped_column(String(120))
    duration_seconds: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Delivery(Base):
    __tablename__ = "deliveries"
    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    next_part: Mapped[int] = mapped_column(Integer, default=0)
    parts: Mapped[list] = mapped_column(JSON, default=list)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ClientPreference(Base):
    __tablename__ = "client_preferences"
    app_mode: Mapped[str] = mapped_column(String(20), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    client_login: Mapped[str] = mapped_column(String(100), index=True)
    selected_counter_ids: Mapped[list] = mapped_column(JSON, default=list)
    primary_goal_ids: Mapped[list] = mapped_column(JSON, default=list)
    goal_roles: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_by: Mapped[str | None] = mapped_column(String(30))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class MetricaCounterCatalog(Base):
    __tablename__ = "metrica_counter_catalog"
    app_mode: Mapped[str] = mapped_column(String(20), primary_key=True)
    counter_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    site: Mapped[str] = mapped_column(String(300), default="")
    permission: Mapped[str] = mapped_column(String(30), default="")
    status: Mapped[str] = mapped_column(String(30), default="unknown")
    goals: Mapped[list] = mapped_column(JSON, default=list)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ClientCounter(Base):
    __tablename__ = "client_counters"
    app_mode: Mapped[str] = mapped_column(String(20), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    counter_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    linked: Mapped[bool] = mapped_column(Boolean, default=False)
    selected: Mapped[bool] = mapped_column(Boolean, default=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SnapshotCache(Base):
    __tablename__ = "snapshot_cache"
    app_mode: Mapped[str] = mapped_column(String(20), primary_key=True)
    client_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    period_start: Mapped[str] = mapped_column(String(10), primary_key=True)
    period_end: Mapped[str] = mapped_column(String(10), primary_key=True)
    quality: Mapped[str] = mapped_column(String(10), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)
    complete: Mapped[bool] = mapped_column(Boolean, default=False)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class ConversationState(Base):
    __tablename__ = "conversation_states"
    app_mode: Mapped[str] = mapped_column(String(20), primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(30), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(30), primary_key=True)
    messages: Mapped[list] = mapped_column(JSON, default=list)
    active_client_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    period: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
