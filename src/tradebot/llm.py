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

    def search(self, system: str, prompt: str, max_searches: int = 3,
               max_continuations: int = 3) -> tuple[str, list[dict]] | None:
        """Let Claude search the web. Returns (Claude's written answer, the actual search
        results as [{title, url, page_age}]). The URLs come from the search tool itself,
        not from Claude's text, so ORION can verify sources against real links."""
        if not self._client:
            return None
        tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches}]
        messages: list[dict] = [{"role": "user", "content": prompt}]
        try:
            response = self._client.messages.create(model=self.model, max_tokens=16000, system=system,
                                                    tools=tools, messages=messages)
            for _ in range(max_continuations):
                if response.stop_reason != "pause_turn":
                    break
                messages = [messages[0], {"role": "assistant", "content": response.content}]
                response = self._client.messages.create(model=self.model, max_tokens=16000, system=system,
                                                        tools=tools, messages=messages)
        except Exception as e:
            log.error("LLM web search failed (%s): %s", type(e).__name__, e)
            return None
        if response.stop_reason == "refusal":
            log.warning("LLM declined web search request")
            return None
        text, results, seen = [], [], set()
        for block in response.content:
            if block.type == "text":
                text.append(block.text)
            elif block.type == "web_search_tool_result" and isinstance(block.content, list):
                for r in block.content:  # an error result is an object, not a list
                    if getattr(r, "url", None) and r.url not in seen:
                        seen.add(r.url)
                        results.append({"title": r.title, "url": r.url, "page_age": getattr(r, "page_age", None)})
        return "\n".join(text), results

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
