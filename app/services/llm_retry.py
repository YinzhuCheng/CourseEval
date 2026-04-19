"""Retry wrapper for LLM calls that return parsed grading JSON."""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable

from app.models import LLMConfig, LLMConfigMember

logger = logging.getLogger(__name__)


def strip_json_fence(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _is_retryable(message: str) -> bool:
    if "HTTP 4" in message:
        return False
    return True


def retry_llm_grading_call(config: LLMConfig | LLMConfigMember, fn: Callable[[], dict], *, label: str = "llm_grading") -> dict:
    """Call ``fn`` (must return parsed dict or raise). Retry on failure with binary exponential backoff."""
    max_attempts = max(1, int(config.max_llm_retries or 3))
    initial = max(1, int(config.llm_retry_initial_seconds or 5))
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except (json.JSONDecodeError, ValueError) as exc:
            last_exc = exc
            if not _is_retryable(str(exc)):
                raise
            logger.warning("%s attempt %s/%s: %s", label, attempt, max_attempts, exc)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if not _is_retryable(str(exc)):
                raise
            logger.warning("%s attempt %s/%s: %s", label, attempt, max_attempts, exc)
        if attempt < max_attempts:
            time.sleep(initial * (2 ** (attempt - 1)))
    raise ValueError(f"{label} failed after {max_attempts} attempts") from last_exc
