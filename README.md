# tradebot

A multi-agent intraday futures trading bot for **Tradovate** and **NinjaTrader 8**,
trading micro index futures (MES, MNQ, MYM, M2K) plus energy and metals (CL/MCL, GC/MGC).

## The desk

| Agent | Job | Output | Brain |
|---|---|---|---|
| **ATLAS** | Market scout | Ranked watchlist | Rules (volatility × relative volume × trend) |
| **VEGA** | Chart analyst | Trend and levels, setup with entry/stop/target | Rules (EMA trend, VWAP, swing levels, ATR) |
| **ORION** | News analyst | Catalysts, source check, news blackout | Claude + trusted-source list + event calendar |
| **LUNA** | Sentiment analyst | Crowd mood and hype risk | Claude (rules fallback) |
| **TITAN** | Risk manager | Approve / revise / reject | **Rules only. No AI can override.** |
| **NOVA** | Head trader | Trade or reject | Claude (rules fallback) |

```
ATLAS ─► VEGA ─► ORION + LUNA ─► NOVA (draft) ─► TITAN ─► NOVA (final) ─► broker(s)
```

NOVA can only say yes or no to the plan TITAN approves. It can't raise size or move the stop.
If Claude is unavailable or declines a request, the AI-powered agents fall back to their rules-only logic.

## TITAN's rules (config/settings.yaml → `risk`)

- 1% of equity max risk per trade (stop distance + 1 tick slippage)
- Daily loss limit: the **stricter** of 20% and the prop firm's daily limit ($1,000 default)
- Prop contract cap in mini-equivalents (Apex 50K: 6 minis = 60 micros) and an eval profit target
  that stops trading once reached
- Prop drawdown: static (Tradeify: fixed line), or trailing (Apex-style): intraday trailing on the highest equity including open P&L
  (or `eod` trailing on end-of-day balances), locking at starting balance + $100. TITAN stops trading
  `drawdown_buffer` dollars *before* the firm's line
- Max trades/day, max open positions, no two same-direction positions in correlated markets
- Minimum reward:risk 1.5, minimum stop distance
- Entries only 09:35–15:30 ET; **everything flattened at 15:50 ET**
- No entries during ORION's high-impact news blackout (10 min before to 15 min after)
- LUNA high hype risk → size halved
- Micros only unless `allow_full_size: true`

Peak equity and start-of-day equity are saved to `logs/`, so restarting the bot doesn't reset the limits.

## Safety switches

- `mode: paper | demo | live` in settings. **Live also requires** `TRADEBOT_LIVE_ARMED=yes-real-money`
  in the environment. Without it, every live order is refused.
- **Kill switch:** `tradebot kill` creates a `KILL` file, flattens all positions and blocks new trades.
  `tradebot resume` removes it.
- If a broker's account balance can't be read, that account skips the cycle (fail closed).
- **Order tracking** (`orders.py`): every order is verified after placement and on every cycle.
  Rejected or unconfirmed entries get their exits cancelled; a position whose stop isn't working
  at the broker is closed; leftover exit orders are cancelled once a position closes; positions
  larger than ordered or not opened by the bot are closed. Records persist across restarts.
- Every agent output and decision is journaled to `logs/journal_YYYY-MM-DD.jsonl`.

## Quick start

```bash
pip install -e ".[ai,dev]"
cp .env.example .env           # add ANTHROPIC_API_KEY (optional) and broker credentials
pytest                         # TITAN + system tests
tradebot once                  # one paper cycle (synthetic prices unless data.source: ninjatrader)
tradebot run                   # continuous loop
```

## Brokers

**Tradovate** (`brokers/tradovate.py`): REST API, demo or live server. Requires Tradovate's API
Access add-on. Orders are market entries with an attached stop + target (OSO bracket), flagged `isAutomated`.
Test with `tradebot check-broker tradovate`.

**NinjaTrader 8** (`brokers/ninjatrader.py`): uses the Automated Trading Interface (order instruction
files). Setup:
1. NinjaTrader → Tools → Options → Automated trading interface → enable **AT Interface**.
2. Install `ninjatrader/TradebotBridge.cs` as an **indicator**: NinjaScript Editor → right-click
   Indicators → New Indicator → name it TradebotBridge → Generate → replace the code with this file →
   Ctrl+S. Add it to one 5 Minute chart (e.g. MES); keep that chart open. It publishes account
   balances and bars for every instrument in its "Other instruments" setting.
3. Set `brokers.ninjatrader.nt_folder`, the `accounts` list (Sim101 and/or your live account, each
   marked `live: true/false`), and contract months (update on roll dates).
   `mode: demo` trades only non-live accounts; `mode: live` trades all of them (needs `TRADEBOT_LIVE_ARMED`).
4. `tradebot check-broker ninjatrader`.

## Backtesting

Replays historical bars through the real desk (ATLAS, VEGA, NOVA rules-only, TITAN, paper fills),
one bar at a time with no look-ahead, and reports win rate, P&L, profit factor, drawdown, worst day,
and whether the prop account would have breached its trailing drawdown.

```bash
# Export 5-minute bars from NinjaTrader: Tools > Historical Data > Export (Minute, 5)
tradebot backtest --csv MES=data/MES.txt --csv MCL=data/MCL.txt --trades
tradebot backtest --synthetic-days 20     # random data, just to see it work
```

News and sentiment aren't replayed, so this tests the chart strategy and the risk rules.

## Data and news

- **Price bars** (`data.source: ninjatrader`): the TradebotBridge indicator (on one open 5 Minute chart) streams 5-minute bars for every
  contract in `brokers.ninjatrader.contracts`. NinjaTrader must be running and connected to a data feed,
  **even when you trade through Tradovate**. Bars stale for 2+ minutes are ignored, so the bot doesn't trade blind.
- **News, calendar and sentiment** (`news.source: web_search`): ORION and LUNA use Claude's web search.
  ORION verifies sources against the URLs the search actually returned, not URLs Claude writes. The economic
  calendar is fetched once per day and drives the news blackout. Results are cached for `refresh_minutes`
  per market group (index, energy, metals) to keep costs down.

## Status

Built: all six agents, pipeline, TITAN + prop rules, paper broker, Tradovate and NinjaTrader connectors,
kill switch, journal, tests.

Round 2: NinjaTrader market data, Claude web-search news/sentiment/calendar, Apex drawdown rules.
The bot refuses demo/live on synthetic (fake) prices.

Round 3: backtester; TITAN now counts risk on open positions against the daily and drawdown limits.

Round 4: Tradeify 10K rules (static drawdown), NinjaTrader routing for prop accounts, live order tracking.

Current setup: Sim101 + a live NinjaTrader account; Tradeify rules kept but disabled (`prop.enabled: false`).

Not yet built: Telegram alerts (postponed), commissions in TITAN's sizing.
