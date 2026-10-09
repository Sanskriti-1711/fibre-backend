"""LLM provider for permit AI advisories.

Thin wrapper that never makes a legal/construction decision. It either
calls a configured chat-completions endpoint (OpenAI-compatible) or returns
None so callers fall back to deterministic templates.

Env:
  PERMITS_LLM_URL      — e.g. https://api.openai.com/v1/chat/completions
  PERMITS_LLM_API_KEY  — bearer token
  PERMITS_LLM_MODEL    — model name (default: gpt-4o-mini)

Nothing in this module touches PermitMatrix readiness/status.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class LlmConfig:
    url: str
    api_key: str
    model: str


def get_llm_config() -> LlmConfig | None:
    url = (os.getenv('PERMITS_LLM_URL') or '').strip()
    key = (os.getenv('PERMITS_LLM_API_KEY') or '').strip()
    if not url or not key:
        return None
    model = (os.getenv('PERMITS_LLM_MODEL') or 'gpt-4o-mini').strip() or 'gpt-4o-mini'
    return LlmConfig(url=url, api_key=key, model=model)


def _strip(s: str) -> str:
    return (s or '').strip()


def chat_completion(
    system: str,
    user: str,
    *,
    max_tokens: int = 900,
    temperature: float = 0.2,
    timeout_s: float = 25.0,
) -> str | None:
    """Call the LLM and return the assistant text, or None on any failure.

    Failure includes: not configured, network error, non-2xx, empty choice.
    Callers must treat None as "use the deterministic fallback".
    """
    cfg = get_llm_config()
    if cfg is None:
        return None
    import requests  # already in requirements

    payload = {
        'model': cfg.model,
        'messages': [
            {'role': 'system', 'content': system},
            {'role': 'user', 'content': user},
        ],
        'temperature': temperature,
        'max_tokens': max_tokens,
    }
    headers = {
        'Authorization': f'Bearer {cfg.api_key}',
        'Content-Type': 'application/json',
    }
    try:
        resp = requests.post(cfg.url, headers=headers, data=json.dumps(payload), timeout=timeout_s)
    except Exception:
        return None
    if resp.status_code < 200 or resp.status_code >= 300:
        return None
    try:
        data = resp.json()
        choices = data.get('choices') or []
        if not choices:
            return None
        msg = choices[0].get('message') or {}
        text = _strip(msg.get('content') or '')
        return text or None
    except Exception:
        return None


AI_DISCLAIMER = (
    "AI-generated draft — verify against the authority's current requirements "
    'before submitting. The permit decision remains deterministic (rule + evidence).'
)
