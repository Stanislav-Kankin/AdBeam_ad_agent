import os
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import Field, SecretStr, ValidationError, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain.clients import Client


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_file_encoding="utf-8")
    app_mode: Literal["mock", "production"] = "production"
    yandex_discover_clients: bool = True
    yandex_client_chat_ids: list[int] = Field(default_factory=list)
    database_url: str = ""
    clients_config: Path = Path("config/clients.yaml")
    telegram_bot_token: SecretStr = SecretStr("")
    telegram_allowed_chat_ids: list[int] = Field(default_factory=list)
    telegram_admin_user_ids: list[int] = Field(default_factory=list)
    telegram_allowed_user_ids: list[int] = Field(default_factory=list)
    telegram_report_chat_id: int | None = None
    schedule_enabled: bool = True
    schedule_hour: int = Field(default=10, ge=0, le=23)
    schedule_minute: int = Field(default=0, ge=0, le=59)
    mock_schedule_interval_seconds: int = Field(default=0, ge=0)
    deepseek_api_key: SecretStr = SecretStr("")
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-v4-flash"
    # Which model answers questions. DeepSeek stays the fallback: switching back is
    # one line in .env.
    llm_provider: Literal["deepseek", "anthropic"] = "deepseek"
    anthropic_api_key: SecretStr = SecretStr("")
    anthropic_model: str = "claude-sonnet-5"
    http_timeout_seconds: float = Field(default=30, gt=0, le=120)
    http_retries: int = Field(default=4, ge=0, le=8)
    max_background_jobs: int = Field(default=4, ge=1, le=16)
    history_retention_days: int = Field(default=90, ge=1, le=3650)
    warehouse_enabled: bool = True
    warehouse_backfill_days: int = Field(default=30, ge=1, le=365)
    warehouse_interval_seconds: int = Field(default=120, ge=30, le=3600)
    warehouse_dimension_interval_seconds: int = Field(default=30, ge=30, le=3600)
    deepseek_daily_limit: int = Field(default=100, ge=1, le=10000)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @model_validator(mode="after")
    def defaults(self):
        if not self.database_url:
            self.database_url = f"sqlite+aiosqlite:///./data/adbeam_{self.app_mode}.db"
        if self.app_mode != "mock" and self.mock_schedule_interval_seconds:
            raise ValueError("Test schedule is allowed only in mock mode")
        return self


def secret_from_env(name: str) -> str:
    # Supports per-client credentials, including custom names in .env.
    return os.environ.get(name, "")


def load_settings() -> Settings:
    load_dotenv(override=False)
    return Settings()


class ClientRegistry:
    def __init__(
        self, clients: list[Client], allowed_chats: list[int], errors=None, overrides=None
    ):
        self.clients = {c.id: c for c in clients}
        self.base_allowed_chats = frozenset(allowed_chats)
        self.user_grants = {}
        self.errors: list[str] = errors or []
        self.overrides = overrides or {}

    @property
    def allowed_chats(self):
        return self.base_allowed_chats | self.user_grants.keys()

    def visible(self, chat_id: int) -> list[Client]:
        if chat_id not in self.allowed_chats:
            return []
        return [
            c
            for c in self.clients.values()
            if c.active
            and (
                c.id in self.user_grants[chat_id]
                if chat_id in self.user_grants
                else chat_id in c.telegram.allowed_chat_ids
            )
        ]

    def require(self, chat_id: int, client_id: str) -> Client:
        for client in self.visible(chat_id):
            if client.id == client_id:
                return client
        raise PermissionError("Клиент не найден или недоступен этому чату.")

    def resolve(self, chat_id: int, query: str) -> list[Client]:
        query = query.casefold().strip()
        visible = self.visible(chat_id)
        exact = [
            c
            for c in visible
            if query in [x.casefold() for x in [c.id, c.name, c.direct.client_login, *c.aliases]]
        ]
        return exact or [
            c
            for c in visible
            if any(query in x.casefold() for x in [c.id, c.name, c.direct.client_login, *c.aliases])
        ]


def load_clients(settings: Settings) -> ClientRegistry:
    if settings.app_mode == "production" and settings.yandex_discover_clients:
        overrides = {}
        if settings.clients_config.exists():
            raw = yaml.safe_load(settings.clients_config.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, dict) or not isinstance(raw.get("clients", []), list):
                raise ValueError("Конфиг должен содержать список clients.")
            for item in raw.get("clients", []):
                if not isinstance(item, dict) or not isinstance(item.get("direct"), dict):
                    raise ValueError("Для настроек клиента требуется direct.client_login.")
                login = item["direct"].get("client_login")
                if not isinstance(login, str) or not login or login in overrides:
                    raise ValueError(
                        "Логины настроек клиентов должны быть непустыми и уникальными."
                    )
                overrides[login] = item
        return ClientRegistry([], settings.telegram_allowed_chat_ids, overrides=overrides)
    path = settings.clients_config
    if not path.exists() and settings.app_mode == "mock":
        path = Path("config/clients.example.yaml")
    if not path.exists():
        raise ValueError("Файл клиентов не найден. Создайте config/clients.yaml.")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not isinstance(raw.get("clients"), list):
        raise ValueError("Конфиг должен содержать список clients.")
    clients, errors, seen, duplicates = [], [], set(), set()
    for index, item in enumerate(raw["clients"]):
        try:
            client = Client.model_validate(item)
            if client.id in seen:
                duplicates.add(client.id)
                raise ValueError("Duplicate client ID")
            seen.add(client.id)
            clients.append(client)
        except (ValidationError, ValueError):
            # No input values or secrets in errors; fail only this entry.
            errors.append(f"Запись клиента #{index + 1}: неверный конфиг или повтор ID.")
    clients = [c for c in clients if c.id not in duplicates]
    return ClientRegistry(clients, settings.telegram_allowed_chat_ids, errors)
