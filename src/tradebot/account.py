"""Tracks start-of-day equity, peak equity and trade count per account, persisted to
disk so a restart can't reset the daily loss limit or the trailing-drawdown peak."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .models import AccountState, Position


class AccountTracker:
    """trailing_type 'intraday': peak = highest equity seen, incl. unrealized (Apex-style).
    trailing_type 'eod': peak = highest end-of-day balance."""

    def __init__(self, key: str, state_dir: str | Path = "logs", trailing_type: str = "intraday",
                 starting_balance: float | None = None):
        self.path = Path(state_dir) / f"state_{key}.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.trailing_type = trailing_type
        self.state = json.loads(self.path.read_text()) if self.path.exists() else {}
        if starting_balance and "peak_intraday" not in self.state:
            self.state["peak_intraday"] = self.state["peak_eod"] = starting_balance

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=2))
        tmp.replace(self.path)

    def update(self, equity: float, positions: list[Position], today: date,
               equity_high: float | None = None) -> AccountState:
        """equity_high: best estimate of the highest equity since the last update (includes
        unrealized gains that came and went between polls). Defaults to equity."""
        st = self.state
        if st.get("date") != today.isoformat():
            if "last_equity" in st:  # close out yesterday for EOD trailing
                st["peak_eod"] = max(st.get("peak_eod", st["last_equity"]), st["last_equity"])
            st.update(date=today.isoformat(), start_of_day_equity=equity, trades_today=0)
        st.setdefault("peak_eod", equity)
        st["peak_intraday"] = max(st.get("peak_intraday", equity), equity, equity_high or equity)
        st["last_equity"] = equity
        self._save()
        peak = st["peak_intraday"] if self.trailing_type == "intraday" else st["peak_eod"]
        return AccountState(equity=equity, start_of_day_equity=st["start_of_day_equity"],
                            peak_equity=peak, trades_today=st["trades_today"], positions=positions)

    def record_trade(self) -> None:
        self.state["trades_today"] = self.state.get("trades_today", 0) + 1
        self._save()
