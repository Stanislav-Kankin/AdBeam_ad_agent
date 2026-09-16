import argparse
import asyncio
import logging
import sys
from pathlib import Path
from time import time

from aiogram import Bot
from aiogram.types import BotCommand
from alembic import command
from alembic.config import Config

from app.analytics.periods import make_period
from app.bot.handlers import build_dispatcher, send_part
from app.config import Settings, load_settings
from app.domain.reports import CheckMode, TriggerSource
from app.runtime import build_runtime
from app.scheduler.jobs import DailySchedule
from app.security import configure_logging

logger = logging.getLogger(__name__)


def migrate(settings):
    config = Config("alembic.ini")
    config.attributes["database_url"] = settings.database_url
    command.upgrade(config, "head")


async def heartbeat():
    path = Path("data/heartbeat")
    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        await asyncio.to_thread(path.write_text, str(time()), encoding="ascii")
        await asyncio.sleep(30)


async def run_bot(runtime):
    settings = runtime.settings
    token = settings.telegram_bot_token.get_secret_value()
    if not token:
        raise ValueError(
            "Укажите TELEGRAM_BOT_TOKEN в .env. Для запуска без ключей: python -m app.main demo"
        )
    if not settings.telegram_allowed_chat_ids:
        raise ValueError("Настройте TELEGRAM_ALLOWED_CHAT_IDS и права клиентов.")
    for error in runtime.registry.errors:
        logger.warning(error)
    async with Bot(token=token) as bot:

        async def send(chat, text):
            await send_part(bot, chat, text)

        runtime.schedule = DailySchedule(settings, runtime.checks, send)
        runtime.schedule.start()
        commands = [
            ("start", "Начать"),
            ("menu", "Главное меню"),
            ("help", "Помощь"),
            ("clients", "Клиенты"),
            ("check_all", "Проверить всех"),
            ("check", "Проверить клиента"),
            ("summary_all", "Краткая сводка"),
            ("summary", "Сводка по клиенту"),
            ("schedule", "Расписание"),
            ("cancel", "Отменить выбор"),
        ]
        await bot.set_my_commands(
            [BotCommand(command=name, description=text) for name, text in commands]
        )
        dp = build_dispatcher(runtime)
        pulse = asyncio.create_task(heartbeat())
        try:
            logger.info("AdBeam started in %s mode", settings.app_mode)
            await dp.start_polling(
                bot, close_bot_session=False, allowed_updates=dp.resolve_used_update_types()
            )
        finally:
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)
            # Drain jobs before the Bot session closes.
            runtime.schedule.close()
            await runtime.jobs.close()


async def run(args, settings):
    runtime = build_runtime(settings)
    try:
        if args.command == "bot":
            await run_bot(runtime)
        elif args.command == "demo":
            if args.ask:
                print(await runtime.agent.ask(args.ask, 123456789, 1))
            elif args.daily:

                async def print_message(chat, text):
                    print(text)

                runtime.schedule = DailySchedule(settings, runtime.checks, print_message)
                await runtime.schedule.run()
            else:
                ids = (
                    [args.client]
                    if args.client
                    else [c.id for c in runtime.registry.visible(123456789)]
                )
                _, text = await runtime.checks.run_check(
                    ids,
                    make_period(args.period),
                    CheckMode.STANDARD,
                    TriggerSource.INTERNAL,
                    chat_id=123456789,
                )
                print(text)
        elif args.command == "validate-config":
            print(
                f"Режим: {settings.app_mode}. Валидных клиентов: {len(runtime.registry.clients)}."
            )
            for error in runtime.registry.errors:
                print(error)
            if runtime.registry.errors:
                raise SystemExit(1)
    finally:
        await runtime.close()


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="AdBeam Performance Analyst")
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("bot")
    subs.add_parser("migrate")
    subs.add_parser("validate-config")
    demo = subs.add_parser(
        "demo", help="Полностью локальная демонстрация без ключей и отправки в Telegram"
    )
    demo.add_argument("--client")
    demo.add_argument("--period", default="7d")
    demo.add_argument("--ask")
    demo.add_argument("--daily", action="store_true")
    demo.add_argument(
        "--live-llm",
        action="store_true",
        help="Один вопрос реальному DeepSeek на mock-данных; требуется --ask и ключ в .env",
    )
    args = parser.parse_args()
    settings = load_settings()
    if args.command == "demo":
        live_key = settings.deepseek_api_key if args.live_llm else ""
        live_model, live_url = settings.deepseek_model, settings.deepseek_base_url
        if args.live_llm and (not args.ask or not settings.deepseek_api_key.get_secret_value()):
            parser.error("--live-llm требует --ask и DEEPSEEK_API_KEY в локальном .env.")
        # Explicitly isolate demo from all production configuration and secrets.
        settings = Settings(
            _env_file=None,
            app_mode="mock",
            database_url="sqlite+aiosqlite:///./data/adbeam_demo.db",
            clients_config=Path("config/clients.example.yaml"),
            telegram_allowed_chat_ids=[123456789],
            telegram_report_chat_id=123456789,
            telegram_bot_token="",
            deepseek_api_key=live_key,
            deepseek_model=live_model,
            deepseek_base_url=live_url,
            schedule_enabled=False,
            mock_schedule_interval_seconds=1,
        )
    configure_logging(settings.log_level)
    try:
        if args.command != "validate-config":
            migrate(settings)
        if args.command != "migrate":
            asyncio.run(run(args, settings))
    except (ValueError, PermissionError) as exc:
        logger.error("%s", exc)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
