#!/usr/bin/env python3
"""Shared retry helper for Gemini API calls in the post-call pipeline.

Used by normalizer.py and extractor.py to transparently retry transient
503/429 errors without changing their public interfaces.

Strategy
--------
- Up to MAX_ATTEMPTS calls total (1 original + MAX_ATTEMPTS-1 retries)
- Exponential backoff: 2s, 4s, 8s, ...
- Only retries on 503 (UNAVAILABLE) and 429 (RESOURCE_EXHAUSTED / rate limit)
- Layer 3: if the primary model keeps 503-ing, try FALLBACK_MODEL once before
  giving up (only when a fallback is configured and different from primary)
"""

from __future__ import annotations

import logging
import time
from typing import Callable, TypeVar

LOGGER = logging.getLogger("voxlayer-pipeline")

MAX_ATTEMPTS = 4          # 1 original + 3 retries
BASE_DELAY_S = 2.0        # seconds; doubles each retry
RETRYABLE_CODES = {503, 429}
RETRYABLE_STRINGS = {"unavailable", "resource_exhausted", "rate limit", "quota"}

T = TypeVar("T")


def _is_retryable(exc: Exception) -> bool:
    msg = str(exc).lower()
    # google-genai raises exceptions whose string form includes the HTTP status
    if any(s in msg for s in RETRYABLE_STRINGS):
        return True
    # Also check for numeric codes in the message
    if any(str(c) in msg for c in RETRYABLE_CODES):
        return True
    return False


def call_with_retry(
    fn: Callable[[], T],
    *,
    label: str = "gemini_call",
    fallback_fn: Callable[[], T] | None = None,
) -> T:
    """Call `fn()`, retrying on transient errors with exponential backoff.

    If `fallback_fn` is provided and all retries of `fn` are exhausted,
    tries `fallback_fn()` once before raising.

    Parameters
    ----------
    fn:           Primary callable (e.g. lambda: client.models.generate_content(...))
    label:        Human-readable label for log messages
    fallback_fn:  Optional callable using the fallback model; called once if
                  all primary retries fail with retryable errors
    """
    last_exc: Exception | None = None
    delay = BASE_DELAY_S

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if not _is_retryable(exc):
                LOGGER.error("%s: non-retryable error on attempt %d: %s", label, attempt, exc)
                raise
            if attempt < MAX_ATTEMPTS:
                LOGGER.warning(
                    "%s: transient error on attempt %d/%d (%s), retrying in %.0fs…",
                    label, attempt, MAX_ATTEMPTS, type(exc).__name__, delay,
                )
                time.sleep(delay)
                delay *= 2
            else:
                LOGGER.error(
                    "%s: all %d attempts failed. Last error: %s",
                    label, MAX_ATTEMPTS, exc,
                )

    # All primary attempts exhausted — try fallback model if provided
    if fallback_fn is not None:
        LOGGER.warning("%s: trying fallback model after %d failures", label, MAX_ATTEMPTS)
        try:
            return fallback_fn()
        except Exception as fb_exc:
            LOGGER.error("%s: fallback model also failed: %s", label, fb_exc)
            raise fb_exc from last_exc

    assert last_exc is not None
    raise last_exc
