import asyncio
import hashlib
import json
import logging
import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from time import monotonic
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)


class IntegrationError(Exception):
    def __init__(self, source: str, code: str):
        self.source, self.code = source, code
        explanation = {
            "quota_cooldown_429": "квота запросов Метрики исчерпана (HTTP 429); запросы временно приостановлены, данные не получены",
            "http_403": "доступ запрещён (HTTP 403)",
        }.get(code, code)
        super().__init__(f"{source}: {explanation if source == 'metrica' else code}")


def number(value) -> Decimal:
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise ValueError
        return result
    except (InvalidOperation, ValueError):
        raise IntegrationError("response", "invalid_number") from None


class ReadTransport:
    """Only adapters know this transport. It is never exposed as a model tool."""

    def __init__(self, client: httpx.AsyncClient, retries: int = 4, sleep=asyncio.sleep):
        self.client, self.retries, self.sleep = client, retries, sleep
        self.metrica_slots = asyncio.Semaphore(2)
        self.metrica_pacing = asyncio.Lock()
        self.metrica_next = 0.0
        self.metrica_interval = 1.7
        self.cooldowns = {}
        self.cache = {}

    async def metrica_request(self, method, url, **kwargs):
        group = "report" if "/stat/" in urlsplit(url).path else "management"
        async with self.metrica_slots:
            if self.cooldowns.get(group, 0) > monotonic():
                raise IntegrationError("metrica", "quota_cooldown_429")
            if group == "report":
                async with self.metrica_pacing:
                    await self.sleep(max(0, self.metrica_next - monotonic()))
                    self.metrica_next = monotonic() + self.metrica_interval
            # A concurrent request may have received 429 while this one waited.
            if self.cooldowns.get(group, 0) > monotonic():
                raise IntegrationError("metrica", "quota_cooldown_429")
            response = await self.client.request(method, url, **kwargs)
            if response.status_code in (420, 429):
                hint = response.headers.get("Retry-After", "")
                pause = max(300, int(hint)) if hint.isdigit() else 300
                self.cooldowns[group] = monotonic() + pause
                # The error type distinguishes parallel, short-term, and daily quotas.
                try:
                    types = [e.get("error_type", "") for e in response.json().get("errors", [])]
                    types = [t for t in types if re.fullmatch(r"[a-zA-Z_]{1,100}", t)]
                except (ValueError, AttributeError, TypeError):
                    types = []
                logger.warning(
                    "Metrica quota exceeded status=%s group=%s type=%s cooldown=%ss",
                    response.status_code,
                    group,
                    ",".join(types) or "unknown",
                    pause,
                )
            return response

    async def request(self, source, method, url, *, pending=False, **kwargs):
        # Log operation only: never headers, query strings, response bodies, or credentials.
        operation = urlsplit(url).path
        for attempt in range(self.retries + 1):
            started = monotonic()
            logger.info(
                "API request source=%s operation=%s attempt=%s", source, operation, attempt + 1
            )
            delay = min(2**attempt, 30)
            try:
                response = await (
                    self.metrica_request(method, url, **kwargs)
                    if source == "metrica"
                    else self.client.request(method, url, **kwargs)
                )
            except httpx.RequestError:
                logger.warning(
                    "API network error source=%s operation=%s elapsed=%.1fs",
                    source,
                    operation,
                    monotonic() - started,
                )
                if attempt == self.retries:
                    raise IntegrationError(source, "network_error") from None
            else:
                logger.info(
                    "API response source=%s operation=%s status=%s elapsed=%.1fs",
                    source,
                    operation,
                    response.status_code,
                    monotonic() - started,
                )
                if response.status_code == 200:
                    return response
                if source == "metrica" and response.status_code in (420, 429):
                    raise IntegrationError(source, "quota_cooldown_429")
                retryable = response.status_code == 429 or response.status_code >= 500
                retryable |= pending and response.status_code in (201, 202)
                if not retryable:
                    raise IntegrationError(source, f"http_{response.status_code}")
                if attempt == self.retries:
                    raise IntegrationError(source, f"retry_exhausted_http_{response.status_code}")
                hint = response.headers.get("retryIn", response.headers.get("Retry-After", ""))
                if hint.isdigit():
                    if int(hint) > 60:
                        # Don't retry earlier than the upstream quota allows.
                        raise IntegrationError(source, "retry_later")
                    delay = max(delay, int(hint))
            await self.sleep(delay)
        raise IntegrationError(source, "unavailable")

    async def json(self, source, method, url, **kwargs):
        cache_key = None
        if source == "metrica" and method == "GET":
            cache_key = hashlib.sha256(
                json.dumps([url, kwargs], sort_keys=True, default=str).encode()
            ).hexdigest()
            cached = self.cache.get(cache_key)
            if cached and cached[0] > monotonic():
                return deepcopy(cached[1])
        response = await self.request(source, method, url, **kwargs)
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError
        except ValueError:
            raise IntegrationError(source, "invalid_response") from None
        if data.get("error") or data.get("errors") or data.get("status") == "error":
            error = data.get("error")
            if isinstance(error, dict):
                code = error.get("error_code")
            else:
                code = data.get("code")
            # Only a numeric API error code is safe to log without echoing input values.
            suffix = f"_{code}" if isinstance(code, int) else ""
            logger.warning(
                "API rejected request source=%s code=%s",
                source,
                code if isinstance(code, int) else "unknown",
            )
            raise IntegrationError(source, "api_error" + suffix)
        if cache_key:
            if len(self.cache) >= 512:
                self.cache.pop(next(iter(self.cache)))
            self.cache[cache_key] = (monotonic() + 300, deepcopy(data))
        return data
