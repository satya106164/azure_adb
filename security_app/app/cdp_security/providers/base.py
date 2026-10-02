from __future__ import annotations

import logging
import time
from typing import Callable

import requests

log = logging.getLogger(__name__)


class ProviderError(Exception):
    """Transient or unknown failure - retried with backoff."""


class PermanentError(ProviderError):
    """Will not succeed on retry (e.g. user not found) - dead-lettered immediately."""


class DeferError(Exception):
    """A prerequisite is not ready yet (e.g. group not synced). Retried without counting an attempt."""


def http(method: str, url: str, token_fn: Callable[[], str], *, ok: tuple[int, ...] = (200, 201, 204),
         tolerate: tuple[int, ...] = (), retries: int = 4, timeout: int = 30, **kwargs) -> requests.Response:
    """HTTP call with bearer auth and retry on 429/5xx honouring Retry-After."""
    for attempt in range(retries):
        headers = {"Authorization": f"Bearer {token_fn()}", "Content-Type": "application/json",
                   **kwargs.pop("headers", {})}
        resp = requests.request(method, url, headers=headers, timeout=timeout, **kwargs)
        if resp.status_code in ok or resp.status_code in tolerate:
            return resp
        if resp.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
            wait = int(resp.headers.get("Retry-After", 2 ** attempt * 2))
            log.warning("%s %s -> %s, retrying in %ss", method, url, resp.status_code, wait)
            time.sleep(min(wait, 60))
            continue
        body = resp.text[:500]
        if resp.status_code in (400, 403, 404):
            raise PermanentError(f"{method} {url} -> {resp.status_code}: {body}")
        raise ProviderError(f"{method} {url} -> {resp.status_code}: {body}")
    raise ProviderError(f"{method} {url}: retries exhausted")
