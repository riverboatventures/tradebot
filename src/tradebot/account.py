"""Tracks start-of-day equity, peak equity and trade count per account, persisted to
disk so a restart can't reset the daily loss limit or the trailing-drawdown peak."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .models import AccountState, Position


class AccountTracker:
    def __init__(self, key: str, state_dir: str | Path = "logs"):
        self.path = Path(state_dir) / f"state_{key}.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {}

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2))
        tmp.replace(self.path)

    def update(self, equity: float, positions: list[Position], today: date) -> AccountState:
        st = self.state
        if st.get("date") != today.isoformat():
            st.update(date=today.isoformat(), start_of_day_equity=equity, trades_today=0)
        st["peak_equity"] = max(st.get("peak_equity", equity), equity)
        self._save()
        return AccountState(equity=equity, start_of_day_equity=st["start_of_day_equity"],
                            peak_equity=st["peak_equity"], trades_today=st["trades_today"],
                            positions=positions)

    def record_trade(self) -> None:
        self.state["trades_today"] = self.state.get("trades_today", 0) + 1
        self._save()
