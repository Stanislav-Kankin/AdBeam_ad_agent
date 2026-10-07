import asyncio
import hashlib
import logging
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.analytics.periods import MOSCOW, AnalysisPeriod, DateRange, make_period, today_moscow
from app.domain.reports import CheckMode, TriggerSource
from app.reporting.formatter import daily_digest, split_message, weekly_digest
from app.storage.backup import backup_sqlite

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
        self.scheduler.add_job(
            self.backup,
            "interval",
            hours=24,
            id="database_backup",
            next_run_time=datetime.now(MOSCOW) + timedelta(minutes=3),
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
                day_of_week=self.settings.schedule_day_of_week,
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

    async def backup(self):
        try:
            await asyncio.to_thread(backup_sqlite, self.settings.database_url)
        except Exception as exc:
            logger.error("Database backup failed (%s)", type(exc).__name__)

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
        if now >= due and (not self.weekly or now.strftime("%a").lower() == self.day):
            await self.run()

    @property
    def day(self):
        return self.settings.schedule_day_of_week

    @property
    def weekly(self):
        return self.day != "*" and not self.settings.mock_schedule_interval_seconds

    @staticmethod
    def last_week():
        today = today_moscow()
        sunday = today - timedelta(days=today.weekday() + 1)
        monday = sunday - timedelta(days=6)
        return AnalysisPeriod(
            current=DateRange(start=monday, end=sunday),
            previous=DateRange(start=monday - timedelta(days=7), end=sunday - timedelta(days=7)),
        )

    async def deliver(self, key, chat, build):
        """Send once per key; a failed send resumes from the last delivered part."""
        repo = self.checks.repository
        delivery = await repo.delivery(key)
        if delivery and delivery.status == "sent":
            return
        if not delivery:
            await repo.save_delivery(key, parts=split_message(await build()))
            delivery = await repo.delivery(key)
        for index in range(delivery.next_part, len(delivery.parts)):
            await self.send(chat, delivery.parts[index])
            await repo.save_delivery(key, next_part=index + 1)
        await repo.save_delivery(key, next_part=len(delivery.parts), status="sent")

    async def run_weekly(self, chat, ids):
        period = self.last_week()
        reports, _ = await self.checks.run_check(
            ids, period, CheckMode.STANDARD, TriggerSource.SCHEDULE, chat_id=chat
        )
        failed = [cid for cid in ids if cid not in {report.client_id for report in reports}]
        week = str(period.current.start)

        async def team():
            return weekly_digest(reports, period, failed)

        await self.deliver(f"{self.settings.app_mode}:{chat}:week:{week}", chat, team)
        # Each manager gets their own clients in a private chat.
        admins = set(self.settings.telegram_admin_user_ids)
        for uid, member in (await self.checks.repository.bot_users()).items():
            mine = [r for r in reports if r.client_id in set(member["client_ids"])]
            if not member["enabled"] or uid in admins or not mine:
                continue

            async def personal(mine=mine):
                return weekly_digest(mine, period, title="Ваши клиенты за неделю")

            try:
                await self.deliver(f"{self.settings.app_mode}:{uid}:week:{week}", uid, personal)
            except Exception as exc:
                # The person may not have opened the bot yet; others still get theirs.
                logger.warning("Personal digest failed user=%s (%s)", uid, type(exc).__name__)

    async def run(self):
        async with self.lock:
            chat = self.settings.telegram_report_chat_id
            if chat not in self.checks.registry.allowed_chats:
                logger.error("Scheduled delivery denied: chat not allowed")
                return
            ids = [c.id for c in self.checks.registry.visible(chat)]
            if self.weekly:
                try:
                    await self.run_weekly(chat, ids)
                except Exception as exc:
                    logger.error(
                        "Weekly digest failed (%s); pending delivery retained",
                        type(exc).__name__,
                    )
                return
            date_key = str(today_moscow())
            if self.settings.mock_schedule_interval_seconds:
                date_key = datetime.now(MOSCOW).isoformat()
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
            f"Сводка: {'включена' if settings.schedule_enabled else 'выключена'}, "
            f"{'по понедельникам за прошлую неделю' if self.day == 'mon' else 'ежедневно' if self.day == '*' else 'день ' + self.day}\n"
            f"Время: {settings.schedule_hour:02}:{settings.schedule_minute:02} Europe/Moscow\n"
            "Проджектам — их клиенты в личку (закрепление: «Пользователи» → «Клиенты»).\n"
            f"Чат доставки: {settings.telegram_report_chat_id}\n"
            f"Последний запуск: {last.started_at.isoformat() + ' (' + last.status + ')' if last else 'не было'}\n"
            f"Следующий запуск: {next_run}\n"
            f"Тестовый интервал: {settings.mock_schedule_interval_seconds or 'выключен'}"
        )

    async def close(self):
        if not self.scheduler.running:
            return
        # APScheduler cancels running coroutine jobs on shutdown but cannot await them.
        # A cancelled purge or warehouse job may still hold a database connection, so it
        # must finish unwinding before the engine is disposed, or closing it deadlocks.
        executor = self.scheduler._executors.get("default")
        running = [f for f in getattr(executor, "_pending_futures", ()) if not f.done()]
        self.scheduler.shutdown(wait=False)
        if running:
            await asyncio.wait(running, timeout=20)
