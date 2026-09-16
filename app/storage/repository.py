import json
from datetime import UTC, datetime

from sqlalchemy import select, update

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

    async def begin_run(self, chat_id, client_ids, period, mode, trigger):
        async with self.sessions.begin() as session:
            run = Run(
                app_mode=self.app_mode,
                chat_id=str(chat_id),
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
