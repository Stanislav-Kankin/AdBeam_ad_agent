import asyncio
import hashlib
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.analytics.periods import MOSCOW, make_period, today_moscow
from app.domain.reports import CheckMode, TriggerSource
from app.reporting.formatter import daily_digest, split_message

logger = logging.getLogger(__name__)


class DailySchedule:
    def __init__(self, settings, checks, send, agent=None):
        self.settings, self.checks, self.send, self.agent = settings, checks, send, agent
        self.scheduler = AsyncIOScheduler(timezone=MOSCOW)
        self.lock = asyncio.Lock()

    def start(self):
        self.scheduler.add_job(
            self.checks.repository.purge,
            "interval",
            hours=24,
            kwargs={"days": self.settings.history_retention_days},
            id="purge_history",
            next_run_time=datetime.now(MOSCOW),
            max_instances=1,
            coalesce=True,
        )
        if (
            self.settings.app_mode == "production"
            and self.settings.warehouse_enabled
            and self.settings.telegram_report_chat_id in self.checks.registry.allowed_chats
        ):
            self.scheduler.add_job(
                self.warm_cache,
                "interval",
                seconds=self.settings.warehouse_interval_seconds,
                id="warehouse_warm",
                next_run_time=datetime.now(MOSCOW) + timedelta(seconds=20),
                coalesce=True,
                max_instances=1,
            )
            self.scheduler.add_job(
                self.warm_dimensions,
                "interval",
                seconds=self.settings.warehouse_dimension_interval_seconds,
                id="dimension_warehouse_warm",
                next_run_time=datetime.now(MOSCOW)
                + timedelta(seconds=self.settings.warehouse_dimension_interval_seconds),
                coalesce=True,
                max_instances=1,
            )
        if not self.settings.schedule_enabled:
            self.scheduler.start()
            return
        chat = self.settings.telegram_report_chat_id
        if chat not in self.checks.registry.allowed_chats:
            raise ValueError("Чат ежедневной доставки должен входить в allowlist.")
        interval = self.settings.mock_schedule_interval_seconds
        trigger = (
            IntervalTrigger(seconds=interval, timezone=MOSCOW)
            if interval
            else CronTrigger(
                hour=self.settings.schedule_hour,
                minute=self.settings.schedule_minute,
                timezone=MOSCOW,
            )
        )
        self.scheduler.add_job(
            self.run,
            trigger=trigger,
            id="daily",
            coalesce=True,
            max_instances=1,
            misfire_grace_time=3600,
        )
        if not interval:
            self.scheduler.add_job(
                self.catch_up,
                "interval",
                minutes=5,
                id="retry_delivery",
                next_run_time=datetime.now(MOSCOW) + timedelta(seconds=5),
                coalesce=True,
                max_instances=1,
            )
        self.scheduler.start()

    async def warm_cache(self):
        try:
            result = await self.checks.warm_next(
                self.settings.telegram_report_chat_id,
                self.settings.warehouse_backfill_days,
            )
            if result:
                logger.info("Warehouse item completed client=%s day=%s", *result)
        except Exception as exc:
            logger.warning("Warehouse item failed (%s); it will be retried", type(exc).__name__)

    async def warm_dimensions(self):
        try:
            result = await self.checks.warm_dimension_next(
                self.settings.telegram_report_chat_id,
                self.settings.warehouse_backfill_days,
            )
            if result:
                logger.info(
                    "Dimension warehouse item completed client=%s day=%s dimension=%s page=%s complete=%s",
                    result[0],
                    result[1],
                    result[2],
                    result[3] + 1,
                    result[4],
                )
        except Exception as exc:
            logger.warning(
                "Dimension warehouse item failed (%s); it will be retried",
                type(exc).__name__,
            )

    async def catch_up(self):
        now = datetime.now(MOSCOW)
        due = now.replace(
            hour=self.settings.schedule_hour,
            minute=self.settings.schedule_minute,
            second=0,
            microsecond=0,
        )
        if now >= due:
            await self.run()

    async def run(self):
        async with self.lock:
            chat = self.settings.telegram_report_chat_id
            if chat not in self.checks.registry.allowed_chats:
                logger.error("Scheduled delivery denied: chat not allowed")
                return
            date_key = str(today_moscow())
            if self.settings.mock_schedule_interval_seconds:
                date_key = datetime.now(MOSCOW).isoformat()
            ids = [c.id for c in self.checks.registry.visible(chat)]
            scope = hashlib.sha256(",".join(sorted(ids)).encode()).hexdigest()[:12]
            key = f"{self.settings.app_mode}:{chat}:{date_key}:{scope}"
            repo = self.checks.repository
            try:
                delivery = await repo.delivery(key)
                if delivery and delivery.status == "sent":
                    return
                if not delivery:
                    results = []
                    for period_name in ("yesterday", "7d"):
                        period = make_period(period_name)
                        reports, _ = await self.checks.run_check(
                            ids, period, CheckMode.STANDARD, TriggerSource.SCHEDULE, chat_id=chat
                        )
                        failed = [
                            f"{cid}: проверка не завершена."
                            for cid in ids
                            if cid not in {r.client_id for r in reports}
                        ]
                        results.append(
                            (
                                "Вчера" if period_name == "yesterday" else "7 дней",
                                reports,
                                period,
                                failed,
                            )
                        )
                    text = daily_digest(results)
                    if self.agent:
                        text, _ = await self.agent.explain_daily_digest(text, chat)
                    if self.checks.registry.errors:
                        text += (
                            f"\n\nВ конфиге пропущено ошибочных записей: "
                            f"{len(self.checks.registry.errors)}. Проверьте журнал запуска."
                        )
                    await repo.save_delivery(key, parts=split_message(text))
                    delivery = await repo.delivery(key)
                for index in range(delivery.next_part, len(delivery.parts)):
                    await self.send(chat, delivery.parts[index])
                    await repo.save_delivery(key, next_part=index + 1)
                await repo.save_delivery(key, next_part=len(delivery.parts), status="sent")
            except Exception as exc:
                logger.error(
                    "Daily check/delivery failed (%s); pending delivery retained",
                    type(exc).__name__,
                )

    async def describe(self):
        settings = self.settings
        last = await self.checks.repository.last_schedule(settings.telegram_report_chat_id)
        job = self.scheduler.get_job("daily") if self.scheduler.running else None
        next_run = job.next_run_time.isoformat() if job and job.next_run_time else "не запланирован"
        return (
            f"Ежедневная проверка: {'включена' if settings.schedule_enabled else 'выключена'}\n"
            f"Время: {settings.schedule_hour:02}:{settings.schedule_minute:02} Europe/Moscow\n"
            f"Чат доставки: {settings.telegram_report_chat_id}\n"
            f"Последний запуск: {last.started_at.isoformat() + ' (' + last.status + ')' if last else 'не было'}\n"
            f"Следующий запуск: {next_run}\n"
            f"Тестовый интервал: {settings.mock_schedule_interval_seconds or 'выключен'}"
        )

    def close(self):
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
