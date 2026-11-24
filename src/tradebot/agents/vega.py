"""VEGA: chart analyst. Outputs trend, key levels, and (if any) a setup."""
from __future__ import annotations

import logging

from .. import indicators as ind
from ..config import Settings
from ..data import MarketData
from ..instruments import get as get_instrument
from ..models import ChartAnalysis, Trend

log = logging.getLogger("VEGA")


def _trend(closes: list[float]) -> Trend:
    if len(closes) < 21:
        return Trend.range
    fast, slow = ind.ema(closes, 9)[-1], ind.ema(closes, 21)[-1]
    gap = (fast - slow) / closes[-1] * 100
    if gap > 0.02 and closes[-1] > slow:
        return Trend.up
    if gap < -0.02 and closes[-1] < slow:
        return Trend.down
    return Trend.range


class Vega:
    """Deterministic: same bars in, same analysis out. Strategy: trade with the
    trend when the higher timeframe agrees and price is on the right side of VWAP.
    Stop goes beyond the nearest swing point; target is the next level or 2R."""

    def __init__(self, settings: Settings, data: MarketData):
        self.s = settings
        self.data = data

    def analyze(self, symbol: str) -> ChartAnalysis:
        inst = get_instrument(symbol)
        bars = self.data.bars(symbol, self.s.session.bar_minutes, 120)
        factor = max(1, self.s.session.higher_tf_minutes // self.s.session.bar_minutes)
        htf = ind.resample(bars, factor)
        closes = [b.close for b in bars]
        last = closes[-1]
        atr = ind.atr(bars)
        session_bars = bars[-78:]  # ~one RTH session of 5m bars
        vwap = ind.vwap(session_bars)
        highs, lows = ind.swing_points(bars[-60:])
        support = sorted({inst.round_price(l) for l in lows if l < last}, reverse=True)[:3]
        resistance = sorted({inst.round_price(h) for h in highs if h > last})[:3]
        trend = _trend(closes)
        htf_trend = _trend([b.close for b in htf])

        a = ChartAnalysis(symbol=symbol, last_price=last, trend=trend, higher_tf_trend=htf_trend,
                          atr=round(atr, 6), vwap=inst.round_price(vwap),
                          support=support, resistance=resistance)

        buf = 2 * inst.tick_size
        if trend == Trend.up and htf_trend != Trend.down and last > vwap:
            stop = support[0] - buf if support and last - support[0] <= 3 * atr else last - 1.5 * atr
            risk = last - stop
            target = next((r for r in resistance if r - last >= 2 * risk), last + 2 * risk)
            a.setup, a.entry, a.stop, a.target = "long", last, inst.round_price(stop), inst.round_price(target)
        elif trend == Trend.down and htf_trend != Trend.up and last < vwap:
            stop = resistance[0] + buf if resistance and resistance[0] - last <= 3 * atr else last + 1.5 * atr
            risk = stop - last
            target = next((s for s in support if last - s >= 2 * risk), last - 2 * risk)
            a.setup, a.entry, a.stop, a.target = "short", last, inst.round_price(stop), inst.round_price(target)

        if a.setup:
            conf = 0.5
            if htf_trend.value == trend.value:
                conf += 0.15
            conf += 0.1  # right side of VWAP (required above)
            conf += 0.15 * min(1.0, abs(ind.trend_strength(closes)) * 5)
            a.confidence = round(min(conf, 0.95), 3)
            a.reasoning = (f"{trend.value} trend on {self.s.session.bar_minutes}m, {htf_trend.value} on "
                           f"{self.s.session.higher_tf_minutes}m, price {'above' if a.setup == 'long' else 'below'} "
                           f"VWAP {a.vwap}. Stop {a.stop}, target {a.target}.")
        else:
            a.reasoning = f"No setup: {trend.value} trend, htf {htf_trend.value}, price vs VWAP {last:.2f}/{vwap:.2f}."
        log.info("%s: %s", symbol, a.reasoning)
        return a
