"""TITAN: risk manager. Outputs approve / revise / reject.

TITAN is pure code. No AI agent can talk it out of a rejection, and nothing
downstream (NOVA, brokers) can increase size or loosen a stop after TITAN signs off.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime

from ..config import Settings
from ..instruments import get as get_instrument
from ..models import (AccountState, NewsReport, RiskDecision, RiskVerdict,
                      SentimentReport, TradeProposal)

log = logging.getLogger("TITAN")

SLIPPAGE_TICKS = 1  # assume the stop fills 1 tick worse than its price


class Titan:
    def __init__(self, settings: Settings):
        self.s = settings
        self.r = settings.risk

    # -- account-level gates (also used by the pipeline to stop the day) --------
    def daily_loss_limit(self, acct: AccountState) -> float:
        limit = acct.start_of_day_equity * self.r.daily_loss_limit_pct / 100
        if self.r.prop.enabled and self.r.prop.daily_loss_limit:
            limit = min(limit, self.r.prop.daily_loss_limit)
        return limit

    def daily_risk_remaining(self, acct: AccountState) -> float:
        # Profits never expand the daily limit; losses shrink it.
        return self.daily_loss_limit(acct) - max(0.0, -acct.day_pnl)

    def drawdown_room(self, acct: AccountState) -> float:
        """$ left before TITAN's own stop line (firm's trailing limit minus buffer)."""
        p = self.r.prop
        if not (p.enabled and p.trailing_drawdown):
            return math.inf
        return acct.equity - self.drawdown_floor(acct) - p.drawdown_buffer

    def drawdown_floor(self, acct: AccountState) -> float:
        """The equity level at which the prop firm fails the account."""
        p = self.r.prop
        floor = acct.peak_equity - p.trailing_drawdown
        if p.trail_lock_offset is not None and p.starting_balance:
            floor = min(floor, p.starting_balance + p.trail_lock_offset)
        return floor

    def contract_weight(self, symbol: str) -> float:
        """How much one contract counts toward the firm's cap (in mini-equivalents)."""
        return self.r.prop.micro_weight if get_instrument(symbol).micro else 1.0

    def trading_halted(self, acct: AccountState) -> str | None:
        p = self.r.prop
        if (p.enabled and p.stop_at_profit_target and p.profit_target and p.starting_balance
                and acct.equity >= p.starting_balance + p.profit_target):
            return f"Profit target reached (${p.profit_target:,.0f}): evaluation passed, no new trades"
        if self.daily_risk_remaining(acct) <= 0:
            return f"Daily loss limit hit (${self.daily_loss_limit(acct):,.0f})"
        if self.drawdown_room(acct) <= 0:
            return "Within drawdown buffer of the prop firm's trailing limit"
        return None

    # -- trade review ---------------------------------------------------------
    def review(self, p: TradeProposal, acct: AccountState, news: NewsReport | None,
               sentiment: SentimentReport | None, now: datetime) -> RiskDecision:
        reasons: list[str] = []

        def reject(why: str) -> RiskDecision:
            reasons.append(why)
            log.info("REJECT %s %s: %s", p.direction, p.symbol, "; ".join(reasons))
            return RiskDecision(verdict=RiskVerdict.reject, proposal=None, reasons=reasons)

        inst = get_instrument(p.symbol)
        sess = self.s.session

        # 1. Hard stops on the whole account
        if halted := self.trading_halted(acct):
            return reject(halted)
        if acct.trades_today >= self.r.max_trades_per_day:
            return reject(f"Max trades per day reached ({self.r.max_trades_per_day})")

        # 2. Time-of-day
        t = now.time()
        if t < sess.first_entry or t > sess.last_entry:
            return reject(f"Outside entry window {sess.first_entry:%H:%M}-{sess.last_entry:%H:%M}")

        # 3. Instrument and exposure
        if not inst.micro and not self.r.allow_full_size:
            return reject(f"{p.symbol} is full-size; only micros allowed")
        if any(pos.symbol == p.symbol for pos in acct.positions):
            return reject(f"Already have a position in {p.symbol}")
        if len(acct.positions) >= self.r.max_open_positions:
            return reject(f"Max open positions reached ({self.r.max_open_positions})")
        sign = 1 if p.direction == "long" else -1
        for pos in acct.positions:
            if get_instrument(pos.symbol).group == inst.group and (pos.quantity > 0) == (sign > 0):
                return reject(f"Already {p.direction} in correlated {inst.group} ({pos.symbol})")

        # 4. News
        if news and news.blackout:
            return reject(f"News blackout: {news.blackout_reason}")

        # 5. Trade geometry
        if (p.stop - p.entry) * sign >= 0:
            return reject("Stop is on the wrong side of entry")
        if (p.target - p.entry) * sign <= 0:
            return reject("Target is on the wrong side of entry")
        stop_ticks = inst.ticks(p.entry - p.stop)
        if stop_ticks < self.r.min_stop_ticks:
            return reject(f"Stop too tight ({stop_ticks:.0f} ticks < {self.r.min_stop_ticks})")
        rr = abs(p.target - p.entry) / abs(p.entry - p.stop)
        if rr < self.r.min_reward_risk:
            return reject(f"Reward:risk {rr:.2f} below minimum {self.r.min_reward_risk}")

        # 6. Position sizing: the smallest of every budget wins
        per_contract = (stop_ticks + SLIPPAGE_TICKS) * inst.tick_value
        # Risk already riding on open positions comes out of the daily and drawdown room,
        # so several trades can't each spend the whole remaining limit.
        budgets = {
            "per-trade risk": acct.equity * self.r.risk_per_trade_pct / 100,
            "daily loss room": self.daily_risk_remaining(acct) - acct.open_risk,
            "drawdown room": self.drawdown_room(acct) - acct.open_risk,
        }
        budget_name, budget = min(budgets.items(), key=lambda kv: kv[1])
        max_qty = int(budget // per_contract)
        max_qty = min(max_qty, self.r.max_contracts_per_trade)
        if self.r.prop.enabled and self.r.prop.max_contracts:
            used = sum(abs(pos.quantity) * self.contract_weight(pos.symbol) for pos in acct.positions)
            room = self.r.prop.max_contracts - used
            max_qty = min(max_qty, int(room / self.contract_weight(p.symbol) + 1e-9))
        if max_qty < 1:
            return reject(f"1 contract risks ${per_contract:,.2f}, more than the {budget_name} budget ${budget:,.2f}")

        qty = min(p.quantity, max_qty) if p.quantity > 0 else max_qty
        if qty < p.quantity:
            reasons.append(f"Size cut {p.quantity} -> {qty} ({budget_name} budget ${budget:,.2f})")
        if sentiment and sentiment.hype_risk == "high":
            cut = int(qty * self.r.hype_risk_size_cut)
            if cut < 1:
                return reject("LUNA flags high hype risk and size can't be reduced further")
            reasons.append(f"Size cut {qty} -> {cut} (LUNA: high hype risk)")
            qty = cut

        approved = p.model_copy(update={"quantity": qty})
        # quantity 0 means "TITAN, you size it": sizing alone is an approval, not a revision.
        verdict = RiskVerdict.approve if p.quantity in (0, qty) else RiskVerdict.revise
        risk_dollars = round(per_contract * qty, 2)
        if not reasons:
            reasons.append("Within all limits")
        log.info("%s %s %s x%d risk=$%.2f R:R=%.2f | %s", verdict.value.upper(), p.direction, p.symbol,
                 qty, risk_dollars, rr, "; ".join(reasons))
        return RiskDecision(verdict=verdict, proposal=approved, reasons=reasons,
                            risk_dollars=risk_dollars, reward_risk=round(rr, 2))
