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
- Prop trailing drawdown: stops trading `drawdown_buffer` dollars *before* the firm's line
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
- Every agent output and decision is journaled to `logs/journal_YYYY-MM-DD.jsonl`.

## Quick start

```bash
pip install -e ".[ai,dev]"
cp .env.example .env           # add ANTHROPIC_API_KEY (optional) and broker credentials
pytest                         # TITAN + system tests
tradebot once                  # one paper cycle on synthetic data
tradebot run                   # continuous loop
```

## Brokers

**Tradovate** (`brokers/tradovate.py`): REST API, demo or live server. Requires Tradovate's API
Access add-on. Orders are market entries with an attached stop + target (OSO bracket), flagged `isAutomated`.
Test with `tradebot check-broker tradovate`.

**NinjaTrader 8** (`brokers/ninjatrader.py`): uses the Automated Trading Interface (order instruction
files). Setup:
1. NinjaTrader → Tools → Options → Automated trading interface → enable **AT Interface**.
2. Copy `ninjatrader/TradebotBridge.cs` to `Documents\NinjaTrader 8\bin\Custom\AddOns\`, open the
   NinjaScript Editor, and press F5. It publishes account balances for TITAN.
3. Set `brokers.ninjatrader.nt_folder`, `account`, and contract months (update on roll dates).
4. `tradebot check-broker ninjatrader`.

## Status

Built: all six agents, pipeline, TITAN + prop rules, paper broker, Tradovate and NinjaTrader connectors,
kill switch, journal, tests.

Not yet built: real market-data feed (currently synthetic bars), real news/sentiment feeds,
economic calendar, backtester, notifications. **Don't run demo or live until the market data feed is real.**
