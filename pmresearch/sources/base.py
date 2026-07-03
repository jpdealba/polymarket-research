"""Shared HTTP plumbing for source adapters: one httpx.Client plus GET-with-
retry/backoff on 429/5xx. Every adapter (dataapi, gamma, subgraph, rpc, clob)
is expected to build on this rather than rolling its own retry logic."""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from typing import Callable

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryConfig:
    max_retries: int = 5
    backoff_base_s: float = 0.5
    backoff_max_s: float = 20.0


class SourceAdapter:
    """A configured httpx.Client plus a GET helper that retries 429/5xx with
    exponential backoff. Non-retryable responses (including expected control-
    flow errors like an offset-cap 400) are returned as-is for the caller to
    interpret — this class only knows about transport-level retryability."""

    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.Client | None = None,
        retry: RetryConfig | None = None,
        timeout: float = 30.0,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self.retry = retry or RetryConfig()
        self._client = client or httpx.Client(base_url=base_url, timeout=timeout)
        self._sleep = sleep_fn

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "SourceAdapter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get_json(self, path: str, params: dict) -> tuple[httpx.Response, object]:
        attempt = 0
        while True:
            response = self._client.get(path, params=params)
            if response.status_code == 429 or response.status_code >= 500:
                attempt += 1
                if attempt > self.retry.max_retries:
                    response.raise_for_status()
                sleep_s = min(
                    self.retry.backoff_max_s, self.retry.backoff_base_s * (2 ** (attempt - 1))
                )
                sleep_s *= 1 + random.random() * 0.1
                logger.warning(
                    "Retrying %s after status %d (attempt %d/%d): sleeping %.2fs",
                    path,
                    response.status_code,
                    attempt,
                    self.retry.max_retries,
                    sleep_s,
                )
                self._sleep(sleep_s)
                continue
            payload = response.json() if response.content else None
            return response, payload
