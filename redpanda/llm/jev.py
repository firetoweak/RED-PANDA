"""TypeSafe Choice boundary; optional personal settings and known service failures."""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


_LOGGER = logging.getLogger(__name__)
ENDPOINT = "https://api.typesafe.ai/v1/systemone"
Action = Literal["continue", "remind", "pause"]


class JevSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)
    enabled: bool
    api_key: str
    model: str
    interval_steps: int = Field(gt=0)
    recent_steps: int = Field(gt=0)
    state_max_tokens: int = Field(ge=1024, le=8000)
    timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    remind_confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    pause_confidence: float = Field(ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def pinned_model(self):
        if not self.model.strip() or self.model in {"jev-latest", "jev-preview"}:
            raise ValueError("Jev 必须使用固定模型版本")
        return self


class JevChoice(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    type: Literal["choice"]
    choice: Action
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    probabilities: dict[Action, float]

    @model_validator(mode="after")
    def distribution(self):
        values = self.probabilities
        if set(values) != {"continue", "remind", "pause"}:
            raise ValueError("incomplete Jev probabilities")
        if any(not math.isfinite(p) or not 0 <= p <= 1 for p in values.values()):
            raise ValueError("invalid Jev probabilities")
        if not math.isclose(sum(values.values()), 1, abs_tol=0.001):
            raise ValueError("invalid Jev probability sum")
        if values[self.choice] != max(values.values()):
            raise ValueError("Jev choice is not the most probable option")
        return self


class _Answers(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    intervention: JevChoice


class _Usage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)


class _Response(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    model: str
    answers: _Answers
    usage: _Usage


class JevClient:
    def __init__(self, path: Path, *, transport=None):
        self.path = path
        self.transport = transport

    def settings(self) -> JevSettings | None:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        settings = JevSettings.model_validate(json.loads(text))
        return settings if settings.enabled and settings.api_key.strip() else None

    async def evaluate(self, state: str, question: dict, settings: JevSettings) -> JevChoice | None:
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=settings.timeout_seconds) as client:
                response = await client.post(
                    ENDPOINT,
                    headers={"Authorization": f"Bearer {settings.api_key}"},
                    json={"model": settings.model, "state": state,
                          "questions": {"intervention": question}},
                )
            response.raise_for_status()
        except (httpx.TransportError, httpx.HTTPStatusError) as error:
            _LOGGER.warning("Jev unavailable: %s", type(error).__name__)
            return None
        try:
            result = _Response.model_validate(response.json())
            if result.model != settings.model:
                raise ValueError("Jev returned a different model version")
        except (json.JSONDecodeError, ValidationError, ValueError) as error:
            _LOGGER.warning("Invalid Jev response: %s", type(error).__name__)
            return None
        answer = result.answers.intervention
        minimum = settings.pause_confidence if answer.choice == "pause" else settings.remind_confidence
        return answer if answer.confidence >= minimum else None
