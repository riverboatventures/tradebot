"""Append-only JSONL log of every agent output and decision, for review and audits."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel


class Journal:
    def __init__(self, log_dir: str | Path = "logs"):
        self.dir = Path(log_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def write(self, agent: str, event: str, payload: BaseModel | dict | None = None, **extra) -> None:
        now = datetime.now(timezone.utc)
        if isinstance(payload, BaseModel):
            payload = payload.model_dump(mode="json")
        rec = {"ts": now.isoformat(), "agent": agent, "event": event, "data": payload, **extra}
        with open(self.dir / f"journal_{now:%Y-%m-%d}.jsonl", "a") as f:
            f.write(json.dumps(rec, default=str) + "\n")
