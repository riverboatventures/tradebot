"""NinjaTrader 8 connector via the Automated Trading Interface (ATI).

How it works:
  * Orders: tradebot writes Order Instruction Files (OIF) into
    <NinjaTrader 8>/incoming/. NinjaTrader picks them up and places the orders.
  * Positions: NinjaTrader writes <instrument> <account>_position.txt into outgoing/.
  * Account balance: the TradebotBridge AddOn (ninjatrader/TradebotBridge.cs)
    writes outgoing/tradebot_account_<account>.json every second. Without it TITAN
    can't see the balance, so this connector refuses to trade (fail closed).

Setup in NinjaTrader: Tools > Options > Automated trading interface > enable "AT Interface".
tradebot must run on the same Windows PC as NinjaTrader (or share the folder).
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path

from ..models import OrderResult, Position, TradeProposal
from .base import Broker, BrokerError

log = logging.getLogger("ninjatrader")

STALE_AFTER_S = 30


class NinjaTraderBroker(Broker):
    name = "ninjatrader"

    def __init__(self, live: bool, live_armed: bool, account: str, nt_folder: str, contracts: dict[str, str]):
        super().__init__(live=live, live_armed=live_armed)
        self.account = account
        self.root = Path(nt_folder)
        self.incoming = self.root / "incoming"
        self.outgoing = self.root / "outgoing"
        self.contracts = contracts

    def nt_instrument(self, symbol: str) -> str:
        month = self.contracts.get(symbol)
        if not month:
            raise BrokerError(f"No contract month configured for {symbol} (brokers.ninjatrader.contracts)")
        return f"{symbol} {month}"

    def connect(self) -> None:
        if not self.incoming.is_dir():
            raise BrokerError(f"NinjaTrader incoming folder not found: {self.incoming}. "
                              "Is NinjaTrader installed and ATI enabled?")
        self.snapshot()  # verifies the bridge AddOn is running
        log.info("Connected to NinjaTrader account %s%s", self.account, " (LIVE)" if self.live else "")

    def _write_oif(self, lines: list[str]) -> None:
        name = f"oif{uuid.uuid4().hex[:12]}.txt"
        tmp = self.incoming / (name + ".tmp")
        tmp.write_text("\n".join(lines) + "\n")
        os.replace(tmp, self.incoming / name)  # atomic: NT never sees a half-written file

    def snapshot(self) -> tuple[float, list[Position]]:
        f = self.outgoing / f"tradebot_account_{self.account}.json"
        if not f.exists():
            raise BrokerError(f"{f.name} missing. Install and enable the TradebotBridge AddOn in NinjaTrader.")
        if time.time() - f.stat().st_mtime > STALE_AFTER_S:
            raise BrokerError(f"{f.name} is stale (>{STALE_AFTER_S}s). Is NinjaTrader running and connected?")
        data = json.loads(f.read_text())
        equity = float(data["NetLiquidation"])
        positions = []
        for sym in self.contracts:
            pf = self.outgoing / f"{self.nt_instrument(sym)} {self.account}_position.txt"
            if not pf.exists():
                continue
            parts = pf.read_text().strip().split(";")  # e.g. LONG;2;6012.25 / FLAT;0;0
            if len(parts) >= 3 and parts[0] in ("LONG", "SHORT") and int(parts[1]):
                qty = int(parts[1]) * (1 if parts[0] == "LONG" else -1)
                positions.append(Position(symbol=sym, quantity=qty, avg_price=float(parts[2])))
        return equity, positions

    def _place_bracket(self, plan: TradeProposal) -> OrderResult:
        inst = self.nt_instrument(plan.symbol)
        entry, exit_ = ("BUY", "SELL") if plan.direction == "long" else ("SELL", "BUY")
        oco = f"tb{uuid.uuid4().hex[:10]}"
        q = plan.quantity
        # PLACE;ACCOUNT;INSTRUMENT;ACTION;QTY;ORDER TYPE;LIMIT;STOP;TIF;OCO ID;ORDER ID;STRATEGY;STRATEGY ID
        self._write_oif([
            f"PLACE;{self.account};{inst};{entry};{q};MARKET;0;0;DAY;;{oco}E;;",
            f"PLACE;{self.account};{inst};{exit_};{q};STOPMARKET;0;{plan.stop};DAY;{oco};{oco}S;;",
            f"PLACE;{self.account};{inst};{exit_};{q};LIMIT;{plan.target};0;DAY;{oco};{oco}T;;",
        ])
        return OrderResult(ok=True, broker=self.name, order_id=f"{oco}E",
                           message="OIF submitted; confirm fill in outgoing/ order state files")

    def flatten_all(self) -> None:
        # CLOSEPOSITION cancels working orders and closes the position for THIS account only.
        # (FLATTENEVERYTHING would hit every account in NinjaTrader, including ones the bot doesn't own.)
        self._write_oif([f"CLOSEPOSITION;{self.account};{self.nt_instrument(s)};DAY" for s in self.contracts])
