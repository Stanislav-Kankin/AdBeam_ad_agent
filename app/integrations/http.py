import asyncio
import logging
from decimal import Decimal, InvalidOperation
from time import monotonic
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger(__name__)


class IntegrationError(Exception):
    def __init__(self, source: str, code: str):
        self.source, self.code = source, code
        super().__init__(f"{source}: {code}")


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
                response = await self.client.request(method, url, **kwargs)
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
                retryable = response.status_code == 429 or response.status_code >= 500
                retryable |= pending and response.status_code in (201, 202)
                if not retryable:
                    raise IntegrationError(source, f"http_{response.status_code}")
                if attempt == self.retries:
                    raise IntegrationError(source, "retry_exhausted")
                hint = response.headers.get("retryIn", response.headers.get("Retry-After", ""))
                if hint.isdigit():
                    if int(hint) > 60:
                        # Don't retry earlier than the upstream quota allows.
                        raise IntegrationError(source, "retry_later")
                    delay = max(delay, int(hint))
            await self.sleep(delay)
        raise IntegrationError(source, "unavailable")

    async def json(self, source, method, url, **kwargs):
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
        return data
