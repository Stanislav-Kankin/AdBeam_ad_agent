import asyncio
from decimal import Decimal, InvalidOperation

import httpx


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
        for attempt in range(self.retries + 1):
            delay = min(2**attempt, 30)
            try:
                response = await self.client.request(method, url, **kwargs)
            except httpx.RequestError:
                if attempt == self.retries:
                    raise IntegrationError(source, "network_error") from None
            else:
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
            raise IntegrationError(source, "api_error")
        return data
