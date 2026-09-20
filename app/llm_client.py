"""The one interface to the LLM. Nothing else imports the Gemini SDK.

Backend is chosen by environment:
  GEMINI_API_KEY set        -> Gemini Developer API (AI Studio key)
  otherwise                 -> Vertex AI on GCP_PROJECT / GEMINI_LOCATION via ADC
                               (works locally after `gcloud auth application-default
                               login`, and on Cloud Run via the service account)

If we hit rate limits or want another model, this file is the only swap point.
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from functools import lru_cache
from typing import Any

from google import genai
from google.genai import errors, types

log = logging.getLogger(__name__)

MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
# "global" spreads load over regions (dynamic shared quota); a single region 429s sooner.
LOCATION = os.environ.get("GEMINI_LOCATION", "global")
PROJECT_ID = os.environ.get("GCP_PROJECT", "sdoc-hackathon")
# 0 = no thinking: classification/extraction prompts are short and structured.
THINKING_BUDGET = int(os.environ.get("GEMINI_THINKING_BUDGET", "0"))

MAX_RETRIES = 8  # ~2 min of backoff: 429s come in bursts under a thread pool
_RETRYABLE = {429, 500, 502, 503, 504}


@lru_cache(maxsize=1)
def _client() -> genai.Client:
    api_key = os.environ.get("GEMINI_API_KEY")
    if api_key:
        log.info("llm_client: Gemini API key backend, model=%s", MODEL)
        return genai.Client(api_key=api_key)
    log.info("llm_client: Vertex AI backend, project=%s location=%s model=%s", PROJECT_ID, LOCATION, MODEL)
    return genai.Client(vertexai=True, project=PROJECT_ID, location=LOCATION)


def backend() -> str:
    return "gemini-api" if os.environ.get("GEMINI_API_KEY") else "vertex-ai"


# A small global throttle so a thread pool can't burst past the quota.
_MIN_INTERVAL = float(os.environ.get("GEMINI_MIN_INTERVAL_S", "0.15"))
_last_call = 0.0
_lock = threading.Lock()


def _throttle() -> None:
    global _last_call
    with _lock:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def generate(
    prompt: str,
    *,
    system: str | None = None,
    json_schema: dict[str, Any] | None = None,
    temperature: float = 0.0,
    max_output_tokens: int = 1024,
) -> str:
    """Return the model's text. With `json_schema`, output is constrained to that
    JSON shape (Gemini response_schema) — callers should still `json.loads` it."""
    config = types.GenerateContentConfig(
        system_instruction=system,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        response_mime_type="application/json" if json_schema else None,
        response_schema=json_schema,
        thinking_config=types.ThinkingConfig(thinking_budget=THINKING_BUDGET),
    )
    delay = 1.0
    for attempt in range(1, MAX_RETRIES + 1):
        _throttle()
        try:
            resp = _client().models.generate_content(model=MODEL, contents=prompt, config=config)
            if not resp.text:
                raise RuntimeError(f"empty response (finish_reason={_finish_reason(resp)})")
            return resp.text
        except errors.APIError as e:
            if e.code not in _RETRYABLE or attempt == MAX_RETRIES:
                raise
            log.warning("llm_client: %s on attempt %d/%d, retrying in %.1fs", e.code, attempt, MAX_RETRIES, delay)
        except RuntimeError:
            if attempt == MAX_RETRIES:
                raise
        time.sleep(delay + random.uniform(0, 0.5))
        delay = min(delay * 2, 30)
    raise RuntimeError("unreachable")


def generate_json(prompt: str, json_schema: dict[str, Any], **kwargs) -> dict[str, Any]:
    """generate() + parse. Raises ValueError if the model returned non-JSON."""
    text = generate(prompt, json_schema=json_schema, **kwargs)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"model returned non-JSON: {text[:200]!r}") from e


def _finish_reason(resp) -> str:
    try:
        return str(resp.candidates[0].finish_reason)
    except Exception:  # noqa: BLE001
        return "unknown"
