import asyncio
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update

from app.security import redact
from app.storage.models import (
    ClientCounter,
    ClientPreference,
    ConversationState,
    DailySnapshot,
    Delivery,
    MetricaCounterCatalog,
    Run,
    SnapshotCache,
    ToolEvent,
)


def safe_json(value):
    value = json.loads(json.dumps(value, ensure_ascii=False, default=str))

    def clean(item):
        if isinstance(item, str):
            return redact(item)
        if isinstance(item, list):
            return [clean(v) for v in item]
        if isinstance(item, dict):
            return {k: clean(v) for k, v in item.items()}
        return item

    return clean(value)


class Repository:
    def __init__(self, sessions, app_mode):
        self.sessions, self.app_mode = sessions, app_mode
        self.model_quota_lock = asyncio.Lock()

    async def reserve_model_call(self, chat_id, user_id, request_id, limit):
        # One bot process per database. Reservation is persisted before the API call.
        today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        async with self.model_quota_lock:
            async with self.sessions.begin() as session:
                used = await session.scalar(
                    select(func.count())
                    .select_from(ToolEvent)
                    .where(
                        ToolEvent.app_mode == self.app_mode,
                        ToolEvent.chat_id == str(chat_id),
                        ToolEvent.tool == "llm_call",
                        ToolEvent.created_at >= today,
                    )
                )
                if used >= limit:
                    return False
                session.add(
                    ToolEvent(
                        request_id=request_id,
                        app_mode=self.app_mode,
                        chat_id=str(chat_id),
                        user_id=str(user_id),
                        tool="llm_call",
                        arguments={},
                        status="reserved",
                        duration_seconds=0,
                    )
                )
                return True

    async def purge(self, days=90):
        cutoff = datetime.now(UTC) - timedelta(days=days)
        async with self.sessions.begin() as session:
            await session.execute(
                delete(Run).where(Run.app_mode == self.app_mode, Run.started_at < cutoff)
            )
            await session.execute(
                delete(ToolEvent).where(
                    ToolEvent.app_mode == self.app_mode, ToolEvent.created_at < cutoff
                )
            )
            await session.execute(
                delete(Delivery).where(
                    Delivery.key.startswith(self.app_mode + ":"),
                    Delivery.updated_at < cutoff,
                    Delivery.status == "sent",
                )
            )
            await session.execute(
                delete(SnapshotCache).where(
                    SnapshotCache.app_mode == self.app_mode,
                    SnapshotCache.expires_at < cutoff,
                )
            )
            await session.execute(
                delete(ConversationState).where(
                    ConversationState.app_mode == self.app_mode,
                    ConversationState.updated_at < cutoff,
                )
            )

    async def cached_snapshot(self, client_id, period, *, quick=False):
        qualities = ["full", "quick"] if quick else ["full"]
        async with self.sessions() as session:
            row = await session.scalar(
                select(SnapshotCache)
                .where(
                    SnapshotCache.app_mode == self.app_mode,
                    SnapshotCache.client_id == client_id,
                    SnapshotCache.period_start == str(period.start),
                    SnapshotCache.period_end == str(period.end),
                    SnapshotCache.quality.in_(qualities),
                    SnapshotCache.expires_at > datetime.now(UTC),
                )
                .order_by(SnapshotCache.quality.asc())
                .limit(1)
            )
        return row.payload if row else None

    async def daily_snapshots(self, client_id, period, *, quick=False):
        qualities = ["full", "quick"] if quick else ["full"]
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(DailySnapshot)
                    .where(
                        DailySnapshot.app_mode == self.app_mode,
                        DailySnapshot.client_id == client_id,
                        DailySnapshot.day >= str(period.start),
                        DailySnapshot.day <= str(period.end),
                        DailySnapshot.quality.in_(qualities),
                        DailySnapshot.refresh_after > datetime.now(UTC),
                    )
                    .order_by(DailySnapshot.day, DailySnapshot.quality.asc())
                )
            ).all()
        by_day = {}
        for row in rows:
            by_day.setdefault(row.day, row.payload)
        if len(by_day) != period.days:
            return None
        return [by_day[str(period.start + timedelta(days=offset))] for offset in range(period.days)]

    async def save_daily_snapshot(self, client_id, period, snapshot, *, quick=False):
        if period.days != 1:
            return
        quality = "quick" if quick else "full"
        key = (self.app_mode, client_id, str(period.start), quality)
        captured = datetime.now(UTC)
        complete = all(
            value.value == "ok" for value in (snapshot.direct.status, snapshot.metrica.status)
        )
        age_days = (datetime.now(UTC).date() - period.end).days
        refresh_in = timedelta(hours=4 if age_days <= 3 else 24 * 30)
        if not complete:
            refresh_in = timedelta(minutes=10)
        async with self.sessions.begin() as session:
            row = await session.get(DailySnapshot, key)
            if row is None:
                row = DailySnapshot(
                    app_mode=key[0],
                    client_id=key[1],
                    day=key[2],
                    quality=key[3],
                    payload={},
                    refresh_after=captured,
                )
                session.add(row)
            row.payload = safe_json(snapshot.model_dump(mode="json"))
            row.complete = complete
            row.captured_at = captured
            row.refresh_after = captured + refresh_in

    async def has_fresh_daily_snapshot(self, client_id, day):
        async with self.sessions() as session:
            return bool(
                await session.scalar(
                    select(func.count())
                    .select_from(DailySnapshot)
                    .where(
                        DailySnapshot.app_mode == self.app_mode,
                        DailySnapshot.client_id == client_id,
                        DailySnapshot.day == str(day),
                        DailySnapshot.quality == "full",
                        DailySnapshot.refresh_after > datetime.now(UTC),
                    )
                )
            )

    async def fresh_daily_keys(self, client_ids, start, end):
        if not client_ids:
            return set()
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(DailySnapshot.client_id, DailySnapshot.day).where(
                        DailySnapshot.app_mode == self.app_mode,
                        DailySnapshot.client_id.in_(client_ids),
                        DailySnapshot.day >= str(start),
                        DailySnapshot.day <= str(end),
                        DailySnapshot.quality == "full",
                        DailySnapshot.refresh_after > datetime.now(UTC),
                    )
                )
            ).all()
        return {(client_id, day) for client_id, day in rows}

    async def save_snapshot(self, client_id, period, snapshot, *, quick=False, ttl_minutes=60):
        quality = "quick" if quick else "full"
        key = (self.app_mode, client_id, str(period.start), str(period.end), quality)
        now = datetime.now(UTC)
        async with self.sessions.begin() as session:
            row = await session.get(SnapshotCache, key)
            if row is None:
                row = SnapshotCache(
                    app_mode=self.app_mode,
                    client_id=client_id,
                    period_start=key[2],
                    period_end=key[3],
                    quality=quality,
                    payload={},
                    expires_at=now,
                )
                session.add(row)
            row.payload = safe_json(snapshot.model_dump(mode="json"))
            row.complete = all(
                value.value == "ok" for value in (snapshot.direct.status, snapshot.metrica.status)
            )
            row.captured_at = now
            row.expires_at = now + timedelta(minutes=ttl_minutes)

    async def conversation(self, chat_id, user_id):
        async with self.sessions() as session:
            row = await session.get(ConversationState, (self.app_mode, str(chat_id), str(user_id)))
            if row is None:
                return {"messages": [], "active_client_id": None, "period": None}
            return {
                "messages": row.messages,
                "active_client_id": row.active_client_id,
                "period": row.period,
            }

    async def save_conversation(
        self, chat_id, user_id, messages=None, *, active_client_id=None, period=None
    ):
        key = (self.app_mode, str(chat_id), str(user_id))
        async with self.sessions.begin() as session:
            row = await session.get(ConversationState, key)
            if row is None:
                row = ConversationState(
                    app_mode=key[0], chat_id=key[1], user_id=key[2], messages=[]
                )
                session.add(row)
            if messages is not None:
                row.messages = safe_json(messages[-6:])
            if active_client_id is not None:
                row.active_client_id = active_client_id
            if period is not None:
                row.period = safe_json(period)
            row.updated_at = datetime.now(UTC)

    async def clear_conversation(self, chat_id, user_id):
        async with self.sessions.begin() as session:
            await session.execute(
                delete(ConversationState).where(
                    ConversationState.app_mode == self.app_mode,
                    ConversationState.chat_id == str(chat_id),
                    ConversationState.user_id == str(user_id),
                )
            )

    async def configure_client(self, client):
        async with self.sessions() as session:
            row = await session.get(ClientPreference, (self.app_mode, client.id))
        if row is None:
            return client
        goals = [str(value) for value in row.primary_goal_ids][:10]
        counters = [int(value) for value in row.selected_counter_ids]
        return client.model_copy(
            update={
                "direct": client.direct.model_copy(update={"main_goal_ids": goals}),
                "metrica": client.metrica.model_copy(
                    update={
                        "counter_id": None,
                        "counter_ids": counters,
                        "main_goal_ids": goals,
                    }
                ),
            }
        )

    async def save_client_preferences(
        self, client, *, counter_ids=None, goal_ids=None, goal_roles=None, user_id=None
    ):
        async with self.sessions.begin() as session:
            row = await session.get(ClientPreference, (self.app_mode, client.id))
            if row is None:
                row = ClientPreference(
                    app_mode=self.app_mode,
                    client_id=client.id,
                    client_login=client.direct.client_login,
                )
                session.add(row)
            if counter_ids is not None:
                row.selected_counter_ids = list(dict.fromkeys(int(v) for v in counter_ids))[:20]
            if goal_ids is not None:
                row.primary_goal_ids = list(dict.fromkeys(str(v) for v in goal_ids))[:10]
            if goal_roles is not None:
                row.goal_roles = goal_roles
            row.updated_by = str(user_id) if user_id is not None else None
            row.updated_at = datetime.now(UTC)
        await self.invalidate_snapshots(client.id)
        return await self.configure_client(client)

    async def invalidate_snapshots(self, client_id):
        async with self.sessions.begin() as session:
            await session.execute(
                delete(SnapshotCache).where(
                    SnapshotCache.app_mode == self.app_mode,
                    SnapshotCache.client_id == client_id,
                )
            )
            await session.execute(
                delete(DailySnapshot).where(
                    DailySnapshot.app_mode == self.app_mode,
                    DailySnapshot.client_id == client_id,
                )
            )

    async def save_counter_catalog(self, counters):
        now = datetime.now(UTC)
        async with self.sessions.begin() as session:
            for value in counters:
                key = (self.app_mode, int(value["id"]))
                row = await session.get(MetricaCounterCatalog, key)
                if row is None:
                    row = MetricaCounterCatalog(app_mode=self.app_mode, counter_id=key[1])
                    session.add(row)
                for field in ("name", "site", "permission", "status"):
                    if field in value:
                        setattr(row, field, str(value.get(field) or "")[:300])
                if "goals" in value:
                    row.goals = safe_json(value["goals"])
                row.checked_at = now

    async def save_client_counters(self, client_id, counters):
        now = datetime.now(UTC)
        async with self.sessions.begin() as session:
            await session.execute(
                delete(ClientCounter).where(
                    ClientCounter.app_mode == self.app_mode,
                    ClientCounter.client_id == client_id,
                )
            )
            session.add_all(
                ClientCounter(
                    app_mode=self.app_mode,
                    client_id=client_id,
                    counter_id=int(value["id"]),
                    linked=bool(value.get("linked")),
                    selected=bool(value.get("selected")),
                    checked_at=now,
                )
                for value in counters
            )

    async def client_counters(self, client_id, *, include_all=False):
        async with self.sessions() as session:
            links = (
                await session.scalars(
                    select(ClientCounter).where(
                        ClientCounter.app_mode == self.app_mode,
                        ClientCounter.client_id == client_id,
                    )
                )
            ).all()
            link_by_id = {row.counter_id: row for row in links}
            query = select(MetricaCounterCatalog).where(
                MetricaCounterCatalog.app_mode == self.app_mode
            )
            if not include_all:
                if not link_by_id:
                    return []
                query = query.where(MetricaCounterCatalog.counter_id.in_(link_by_id))
            rows = (await session.scalars(query.order_by(MetricaCounterCatalog.name))).all()
        return [
            {
                "id": row.counter_id,
                "name": row.name,
                "site": row.site,
                "permission": row.permission,
                "status": row.status,
                "goals": row.goals,
                "linked": bool(
                    link_by_id.get(row.counter_id) and link_by_id[row.counter_id].linked
                ),
                "selected": bool(
                    link_by_id.get(row.counter_id) and link_by_id[row.counter_id].selected
                ),
                "checked_at": row.checked_at,
            }
            for row in rows
        ]

    async def begin_run(self, chat_id, client_ids, period, mode, trigger, user_id=None):
        async with self.sessions.begin() as session:
            run = Run(
                app_mode=self.app_mode,
                chat_id=str(chat_id),
                user_id=str(user_id) if user_id is not None else None,
                client_ids=client_ids,
                period=period.model_dump(mode="json"),
                mode=str(mode),
                trigger=str(trigger),
            )
            session.add(run)
            await session.flush()
            return run.id

    async def finish_run(self, run_id, reports, errors, text, duration, status=None):
        async with self.sessions.begin() as session:
            await session.execute(
                update(Run)
                .where(Run.id == run_id)
                .values(
                    reports=safe_json([r.model_dump(mode="json") for r in reports]),
                    errors=safe_json(errors),
                    report_text=redact(text),
                    status=status or ("partial" if errors else "completed"),
                    finished_at=datetime.now(UTC),
                    duration_seconds=duration,
                )
            )

    async def tool_event(self, **kwargs):
        async with self.sessions.begin() as session:
            session.add(ToolEvent(app_mode=self.app_mode, **safe_json(kwargs)))

    async def last_schedule(self, chat_id):
        async with self.sessions() as session:
            return await session.scalar(
                select(Run)
                .where(
                    Run.app_mode == self.app_mode,
                    Run.chat_id == str(chat_id),
                    Run.trigger == "schedule",
                )
                .order_by(Run.started_at.desc())
                .limit(1)
            )

    async def delivery(self, key):
        async with self.sessions() as session:
            return await session.get(Delivery, key)

    async def save_delivery(self, key, parts=None, next_part=0, status="pending"):
        async with self.sessions.begin() as session:
            row = await session.get(Delivery, key)
            if row is None:
                row = Delivery(key=key, parts=parts or [])
                session.add(row)
            if parts is not None:
                row.parts = parts
            row.next_part, row.status, row.updated_at = next_part, status, datetime.now(UTC)
