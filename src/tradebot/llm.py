"""Thin wrapper around Claude for the AI-powered agents (ORION, LUNA, NOVA).

Every call returns a validated pydantic object, or None. None means "the AI was
unavailable or declined" and each agent then falls back to its rules-only logic.
For a trading bot, the safe fallback is always "no new information / no trade".
"""
from __future__ import annotations

import logging
import os
from typing import TypeVar

from pydantic import BaseModel

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class LLM:
    def __init__(self, model: str = "claude-opus-5-5", enabled: bool = True):
        self.model = model
        self._client = None
        if not enabled:
            log.info("LLM disabled in settings; ORION/LUNA/NOVA run rules-only")
            return
        if not os.environ.get("ANTHROPIC_API_KEY"):
            log.warning("ANTHROPIC_API_KEY not set; ORION/LUNA/NOVA run rules-only")
            return
        try:
            import anthropic
        except ImportError:
            log.warning("anthropic SDK not installed (pip install -e '.[ai]'); rules-only mode")
            return
        self._client = anthropic.Anthropic()

    @property
    def available(self) -> bool:
        return self._client is not None

    def structured(self, system: str, prompt: str, schema: type[T], max_tokens: int = 8000) -> T | None:
        if not self._client:
            return None
        try:
            response = self._client.messages.parse(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": prompt}],
                output_format=schema,
            )
        except Exception as e:  # network/API errors must never crash the trading loop
            log.error("LLM call failed (%s): %s", type(e).__name__, e)
            return None
        if response.stop_reason == "refusal":
            log.warning("LLM declined request; agent falls back to rules-only")
            return None
        return response.parsed_output
