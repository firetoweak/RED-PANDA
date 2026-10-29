"""Documented OpenAI capabilities not supplied by GET /models.

Model availability always comes from the account's catalog. Unknown models do
not acquire reasoning controls from a guessed family or provider-wide default.
https://developers.openai.com/api/docs/models/gpt-6.1-terra
https://developers.openai.com/api/docs/guides/reasoning
"""
from __future__ import annotations

import re


_LEVELS = ("low", "medium", "high", "xhigh", "max")
_OPENAI = {
    "gpt-6.1-terra": (("none", *_LEVELS), "medium", 1_050_000),
    "gpt-6.1-sol": (_LEVELS, "medium", 1_050_000),
    "gpt-6-astra": (_LEVELS, "medium", 1_050_000),
    "gpt-6-sol": (("none", *_LEVELS), "medium", 1_050_000),
    "gpt-6-luna": (("none", *_LEVELS), "medium", 1_050_000),
    "gpt-5.6-sol": (("none", *_LEVELS), "medium", None),
    "gpt-5.6-terra": (("none", *_LEVELS), "medium", None),
    "gpt-5.6-luna": (("none", *_LEVELS), "medium", None),
}


def openai_metadata(model: str) -> dict:
    canonical = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", model)
    levels, default, context = _OPENAI.get(canonical, ((), None, None))
    return {"reasoning_levels": list(levels), "default_reasoning_effort": default,
            "context_window": context, "web_search": canonical in _OPENAI or canonical in {"gpt-4.1", "gpt-4.1-mini"}}


def openai_conversation_model(model: str) -> bool:
    return (model.startswith(("gpt-5", "gpt-6", "gpt-4.1", "gpt-4o", "o1", "o3", "o4"))
            and not any(part in model for part in ("audio", "realtime", "transcribe", "tts", "search", "instruct")))
