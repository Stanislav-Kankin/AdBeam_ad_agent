import asyncio
import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update

from app.security import redact
from app.storage.models import Delivery, Run, ToolEvent


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
