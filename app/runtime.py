from dataclasses import dataclass

import httpx

from app.agent.service import AgentService
from app.analytics.diagnostics import CheckService
from app.bot.jobs import BackgroundJobs
from app.config import load_clients
from app.integrations.deepseek import DeepSeekProvider
from app.integrations.direct import DirectAdapter
from app.integrations.http import ReadTransport
from app.integrations.metrica import MetricaAdapter
from app.integrations.mock import MockProvider
from app.integrations.offline_llm import OfflineDemoProvider
from app.integrations.provider import ProductionProvider
from app.integrations.roistat import RoistatAdapter
from app.storage.database import database
from app.storage.repository import Repository


@dataclass
class Runtime:
    settings: object
    registry: object
    checks: CheckService
    agent: AgentService
    jobs: BackgroundJobs
    engine: object
    http: httpx.AsyncClient
    schedule: object = None

    async def close(self):
        if self.schedule:
            self.schedule.close()
        await self.jobs.close()
        if isinstance(self.agent.llm, DeepSeekProvider):
            await self.agent.llm.close()
        await self.http.aclose()
        await self.engine.dispose()


def build_runtime(settings):
    registry = load_clients(settings)
    engine, sessions = database(settings.database_url)
    http = httpx.AsyncClient(timeout=settings.http_timeout_seconds, follow_redirects=False)
    transport = ReadTransport(http, settings.http_retries)
    provider = (
        MockProvider()
        if settings.app_mode == "mock"
        else ProductionProvider(
            DirectAdapter(transport), MetricaAdapter(transport), RoistatAdapter(transport)
        )
    )
    repo = Repository(sessions, settings.app_mode)
    checks = CheckService(registry, provider, repo)
    llm = (
        DeepSeekProvider(settings)
        if settings.deepseek_api_key.get_secret_value()
        else (OfflineDemoProvider() if settings.app_mode == "mock" else None)
    )
    return Runtime(
        settings,
        registry,
        checks,
        AgentService(checks, llm),
        BackgroundJobs(settings.max_background_jobs),
        engine,
        http,
    )
