"""Shared async HTTP client with retry and rate limiting."""
import asyncio
import httpx
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type


def make_client(headers: dict = None, auth=None, timeout: float = 30.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers=headers or {},
        auth=auth,
        timeout=timeout,
        follow_redirects=True,
    )


async def safe_get(client: httpx.AsyncClient, url: str, params: dict = None,
                   retries: int = 3) -> dict | None:
    """GET with exponential backoff on 429/5xx."""
    for attempt in range(retries):
        try:
            resp = await client.get(url, params=params)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 60))
                await asyncio.sleep(retry_after)
                continue
            if resp.status_code >= 500:
                await asyncio.sleep(2 ** attempt)
                continue
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()
        except httpx.TimeoutException:
            await asyncio.sleep(2 ** attempt)
        except Exception:
            if attempt == retries - 1:
                raise
            await asyncio.sleep(2 ** attempt)
    return None


async def safe_post(client: httpx.AsyncClient, url: str, json: dict = None,
                    retries: int = 3) -> dict | None:
    """POST with exponential backoff."""
    for attempt in range(retries):
        try:
            resp = await client.post(url, json=json)
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 60))
                await asyncio.sleep(retry_after)
                continue
            if resp.status_code >= 500:
                await asyncio.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            return resp.json()
        except httpx.TimeoutException:
            await asyncio.sleep(2 ** attempt)
        except Exception:
            if attempt == retries - 1:
                raise
            await asyncio.sleep(2 ** attempt)
    return None
