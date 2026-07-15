"""Base connector class with retry logic, rate limiting, and logging."""

import time
import logging
from typing import Optional, Any
import httpx

logger = logging.getLogger(__name__)


def parse_retry_after(raw: Any, default: int = 2, cap: int = 60) -> int:
    """Parse a Retry-After header value tolerantly.

    Shopify manda "2.0" (stringa float): int("2.0") solleva ValueError e
    l'eccezione buca gli except httpx del retry loop, abbattendo l'intero
    sync del chiamante. Qui qualsiasi valore non numerico degrada al default.
    """
    try:
        seconds = int(float(raw))
    except (TypeError, ValueError):
        return default
    return max(1, min(seconds, cap))


class BaseConnector:
    """Base class for all API connectors."""

    def __init__(self, base_url: str, timeout: int = 30, max_retries: int = 3):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.client = httpx.Client(timeout=timeout)
        # Header dell'ultima risposta riuscita di _request: serve ai
        # connettori REST che paginano via header Link (Shopify).
        self.last_response_headers: Optional[httpx.Headers] = None

    def _request(
        self,
        method: str,
        endpoint: str,
        headers: Optional[dict] = None,
        params: Optional[dict] = None,
        json_data: Optional[dict] = None,
        data: Optional[Any] = None,
    ) -> dict:
        """Make HTTP request with retry logic."""
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        last_error = None

        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=params,
                    json=json_data,
                    data=data,
                )

                # Rate limit handling
                if response.status_code == 429:
                    retry_after = parse_retry_after(response.headers.get("Retry-After"))
                    logger.warning(f"Rate limited on {url}. Waiting {retry_after}s (attempt {attempt})")
                    time.sleep(retry_after)
                    continue

                response.raise_for_status()
                self.last_response_headers = response.headers

                # Return JSON if possible, otherwise raw text
                try:
                    return response.json()
                except Exception:
                    return {"raw": response.text}

            except httpx.HTTPStatusError as e:
                last_error = e
                logger.error(f"HTTP {e.response.status_code} on {url} (attempt {attempt}): {e.response.text[:200]}")
                if e.response.status_code >= 500:
                    time.sleep(2 ** attempt)
                    continue
                raise
            except httpx.RequestError as e:
                last_error = e
                logger.error(f"Request error on {url} (attempt {attempt}): {e}")
                time.sleep(2 ** attempt)
                continue

        raise last_error or Exception(f"Failed after {self.max_retries} retries: {url}")

    def get(self, endpoint: str, **kwargs) -> dict:
        return self._request("GET", endpoint, **kwargs)

    def post(self, endpoint: str, **kwargs) -> dict:
        return self._request("POST", endpoint, **kwargs)

    def put(self, endpoint: str, **kwargs) -> dict:
        return self._request("PUT", endpoint, **kwargs)

    def close(self):
        self.client.close()
