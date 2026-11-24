"""Tradovate REST connector (demo and live servers).

Requires Tradovate's API Access add-on. Credentials come from the environment (.env).
Docs: https://api.tradovate.com

Before going live, run `tradebot check-broker tradovate` against the DEMO server and
confirm on the Tradovate platform that the stop and target show up as an OCO pair.
"""
from __future__ import annotations

import logging
import os
import time

import requests

from ..models import OrderResult, Position, TradeProposal
from .base import Broker, BrokerError

log = logging.getLogger("tradovate")

URLS = {
    "demo": "https://demo.tradovateapi.com/v1",
    "live": "https://live.tradovateapi.com/v1",
}


class TradovateBroker(Broker):
    name = "tradovate"

    def __init__(self, live: bool, live_armed: bool, account_spec: str = "", timeout: float = 10.0):
        super().__init__(live=live, live_armed=live_armed)
        self.base = URLS["live" if live else "demo"]
        self.account_spec = account_spec
        self.account_id: int | None = None
        self.timeout = timeout
        self._token: str | None = None
        self._token_expiry = 0.0
        self._contract_names: dict[int, str] = {}  # contractId -> "MESZ6"
        self._front_month: dict[str, str] = {}    # "MES" -> "MESZ6"
        self.http = requests.Session()

    # -- auth -------------------------------------------------------------------
    def _auth(self) -> None:
        env = os.environ
        missing = [k for k in ("TRADOVATE_USERNAME", "TRADOVATE_PASSWORD", "TRADOVATE_CID", "TRADOVATE_SECRET")
                   if not env.get(k)]
        if missing:
            raise BrokerError(f"Tradovate credentials missing: {', '.join(missing)}")
        body = {
            "name": env["TRADOVATE_USERNAME"], "password": env["TRADOVATE_PASSWORD"],
            "appId": env.get("TRADOVATE_APP_ID", "tradebot"), "appVersion": "0.1",
            "cid": env["TRADOVATE_CID"], "sec": env["TRADOVATE_SECRET"],
            "deviceId": env.get("TRADOVATE_DEVICE_ID", "tradebot"),
        }
        r = self.http.post(f"{self.base}/auth/accesstokenrequest", json=body, timeout=self.timeout)
        data = r.json()
        if "accessToken" not in data:
            raise BrokerError(f"Tradovate auth failed: {data.get('errorText') or data}")
        self._token = data["accessToken"]
        self._token_expiry = time.time() + 75 * 60  # tokens last ~90 min; renew early
        self.http.headers["Authorization"] = f"Bearer {self._token}"

    def _ensure_token(self) -> None:
        if not self._token or time.time() > self._token_expiry:
            self._auth()

    def _get(self, path: str, **params):
        self._ensure_token()
        r = self.http.get(f"{self.base}{path}", params=params, timeout=self.timeout)
        if r.status_code != 200:
            raise BrokerError(f"GET {path} -> {r.status_code} {r.text[:200]}")
        return r.json()

    def _post(self, path: str, body: dict):
        self._ensure_token()
        r = self.http.post(f"{self.base}{path}", json=body, timeout=self.timeout)
        if r.status_code != 200:
            raise BrokerError(f"POST {path} -> {r.status_code} {r.text[:200]}")
        return r.json()

    # -- Broker interface -------------------------------------------------------
    def connect(self) -> None:
        self._auth()
        accounts = self._get("/account/list")
        if not accounts:
            raise BrokerError("No Tradovate accounts found")
        if self.account_spec:
            match = [a for a in accounts if a["name"] == self.account_spec]
            if not match:
                raise BrokerError(f"Account {self.account_spec!r} not found; have {[a['name'] for a in accounts]}")
            acct = match[0]
        elif len(accounts) == 1:
            acct = accounts[0]
        else:
            raise BrokerError(f"Multiple accounts; set brokers.tradovate.account_spec to one of "
                              f"{[a['name'] for a in accounts]}")
        self.account_id, self.account_spec = acct["id"], acct["name"]
        log.info("Connected to Tradovate %s account %s", "LIVE" if self.live else "DEMO", self.account_spec)

    def front_month(self, root: str) -> str:
        if root not in self._front_month:
            res = self._get("/contract/suggest", t=root, l=1)
            if not res:
                raise BrokerError(f"No Tradovate contract for {root}")
            self._front_month[root] = res[0]["name"]
        return self._front_month[root]

    def _contract_name(self, contract_id: int) -> str:
        if contract_id not in self._contract_names:
            self._contract_names[contract_id] = self._get("/contract/item", id=contract_id)["name"]
        return self._contract_names[contract_id]

    @staticmethod
    def _root(contract_name: str) -> str:
        return contract_name[:-2]  # "MESZ6" -> "MES"

    def snapshot(self) -> tuple[float, list[Position]]:
        snap = self._post("/cashBalance/getcashbalancesnapshot", {"accountId": self.account_id})
        cash = snap.get("totalCashValue")
        if cash is None:
            raise BrokerError(f"Unexpected cash balance snapshot: {snap}")
        equity = float(cash) + float(snap.get("openPnL", 0.0) or 0.0)
        positions = []
        for p in self._get("/position/list"):
            if p.get("accountId") != self.account_id or not p.get("netPos"):
                continue
            positions.append(Position(symbol=self._root(self._contract_name(p["contractId"])),
                                      quantity=int(p["netPos"]), avg_price=float(p.get("netPrice") or 0.0)))
        return equity, positions

    def _place_bracket(self, plan: TradeProposal) -> OrderResult:
        entry_side, exit_side = ("Buy", "Sell") if plan.direction == "long" else ("Sell", "Buy")
        body = {
            "accountSpec": self.account_spec,
            "accountId": self.account_id,
            "action": entry_side,
            "symbol": self.front_month(plan.symbol),
            "orderQty": plan.quantity,
            "orderType": "Market",
            "isAutomated": True,  # required by CME rules for algorithmic orders
            "bracket1": {"action": exit_side, "orderType": "Stop", "stopPrice": plan.stop},
            "bracket2": {"action": exit_side, "orderType": "Limit", "price": plan.target},
        }
        res = self._post("/order/placeOSO", body)
        if res.get("failureReason") or "orderId" not in res:
            return OrderResult(ok=False, broker=self.name, message=str(res))
        # bracket order ids (field names to be confirmed on the demo server; if absent, the
        # tracker falls back to looking for any working order on the contract)
        stop_id, target_id = res.get("oso1Id"), res.get("oso2Id")
        return OrderResult(ok=True, broker=self.name, order_id=str(res["orderId"]),
                           stop_id=str(stop_id) if stop_id else None,
                           target_id=str(target_id) if target_id else None)

    _WORKING = {"Working", "PendingNew", "Suspended", "PendingReplace"}

    def bracket_status(self, bracket):
        from ..orders import BracketStatus
        ids = [i for i in (bracket.entry_id, bracket.stop_id, bracket.target_id) if i]
        orders = {str(o["id"]): o for o in self._get("/order/items", ids=",".join(ids))}
        es = orders.get(bracket.entry_id, {}).get("ordStatus", "")
        entry = {"Filled": "filled", "Rejected": "rejected", "Canceled": "cancelled",
                 "Expired": "cancelled", "": "unknown"}.get(es, "pending")
        if bracket.stop_id:
            stop_ok = orders.get(bracket.stop_id, {}).get("ordStatus") in self._WORKING
            target_ok = orders.get(bracket.target_id or "", {}).get("ordStatus") in self._WORKING
        else:  # bracket ids unknown: accept any working order on this contract as protection
            symbol = self.front_month(bracket.symbol)
            working = [o for o in self._get("/order/list")
                       if o.get("accountId") == self.account_id and o.get("ordStatus") in self._WORKING
                       and self._contract_name(o["contractId"]) == symbol]
            stop_ok = target_ok = bool(working)
        return BracketStatus(entry=entry, stop_working=stop_ok, target_working=target_ok)

    def cancel_order(self, order_id: str) -> None:
        self._post("/order/cancelorder", {"orderId": int(order_id), "isAutomated": True})

    def close_position(self, symbol: str) -> None:
        name = self.front_month(symbol)
        for p in self._get("/position/list"):
            if p.get("accountId") == self.account_id and p.get("netPos") and self._contract_name(p["contractId"]) == name:
                self._post("/order/liquidateposition",
                           {"accountId": self.account_id, "contractId": p["contractId"], "admin": False})

    def flatten_all(self) -> None:
        for o in self._get("/order/list"):
            if o.get("accountId") == self.account_id and o.get("ordStatus") in ("Working", "Suspended", "PendingNew"):
                try:
                    self._post("/order/cancelorder", {"orderId": o["id"], "isAutomated": True})
                except BrokerError as e:
                    log.error("cancel %s failed: %s", o["id"], e)
        for p in self._get("/position/list"):
            if p.get("accountId") == self.account_id and p.get("netPos"):
                self._post("/order/liquidateposition",
                           {"accountId": self.account_id, "contractId": p["contractId"], "admin": False})
