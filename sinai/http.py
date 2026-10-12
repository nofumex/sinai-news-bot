import asyncio
import random
import time
import httpx


class APIError(Exception):
    def __init__(self, service, status, code=""):
        self.service, self.status, self.code = service, status, code
        super().__init__(f"{service}: HTTP {status} {code}")


class RetryLater(APIError):
    def __init__(self, service, seconds=30):
        self.seconds = max(1, min(float(seconds), 86400))
        super().__init__(service, 429)


class AmbiguousSend(APIError):
    """Request may have reached the server: never blindly repeat a message."""


class HTTP:
    def __init__(self, service, *, client=None, interval=0.25, verify=True):
        self.service = service
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(30, connect=10), verify=verify)
        self.interval = interval
        self.lock = asyncio.Lock()
        self.last = 0.0

    async def close(self):
        await self.client.aclose()

    async def request(self, method, url, *, safe=True, **kwargs):
        for attempt in range(5 if safe else 1):
            async with self.lock:
                await asyncio.sleep(max(0, self.last + self.interval - time.monotonic()))
                self.last = time.monotonic()
                try:
                    response = await self.client.request(method, url, **kwargs)
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    if not safe:
                        raise RetryLater(self.service, 30) from None
                    response = None
                except httpx.TransportError:
                    if not safe:
                        raise AmbiguousSend(self.service, 0, "transport_outcome_unknown") from None
                    response = None
            if response is not None:
                if response.status_code == 429:
                    try:
                        delay = response.json().get("parameters", {}).get("retry_after", response.headers.get("Retry-After", 30))
                        raise RetryLater(self.service, float(delay))
                    except (ValueError, TypeError):
                        raise RetryLater(self.service) from None
                if response.status_code < 500:
                    if response.status_code >= 400:
                        # No URLs/body in errors: Telegram URLs contain the bot credential.
                        raise APIError(self.service, response.status_code)
                    try:
                        return response.json() if response.content else {}
                    except ValueError:
                        if not safe:
                            raise AmbiguousSend(self.service, response.status_code, "invalid_response_outcome_unknown") from None
                        raise APIError(self.service, response.status_code, "invalid_json") from None
                if not safe:
                    raise AmbiguousSend(self.service, response.status_code, "server_outcome_unknown")
            if attempt < 4:
                await asyncio.sleep(min(15, 2 ** attempt + random.random()))
        raise APIError(self.service, 503, "retries_exhausted")
