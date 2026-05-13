# XAUUSD Anchor Strategy Bot

Python + MetaTrader 5 bot that trades XAUUSD using a 02:00 broker-time anchor with OCO pending stop orders, fixed TP/SL, and a progressive 9-step trailing lock.

## Strategy

- **Anchor**: Open price of the 02:00 broker-time M5 candle, captured fresh each day.
- **Entry (OCO)**: Buy Stop at anchor + $10, Sell Stop at anchor − $10. When one fires, the other is cancelled.
- **TP**: ±$3 from trigger.
- **SL**: ±$15 from trigger.
- **Trailing**: Every $0.30 of favorable movement, SL ratchets up by $0.30. Nine lock steps total (+$0.30 → +$2.70). Never moves backwards.
- **EOD cleanup**: Cancels any unfilled pending orders at 23:00 broker time.
- **Weekends**: Skipped automatically.

## Project Structure

```
xauusd_anchor_bot/
├── main.py                   # Entry point
├── config.py                 # All settings
├── requirements.txt
├── README.md
├── core/
│   ├── logger.py             # Logging setup
│   ├── mt5_client.py         # MT5 connection wrappers
│   ├── time_utils.py         # Time helpers
│   ├── telegram_notifier.py  # Telegram alerts (safe — never crashes)
│   └── safety.py             # Spread, daily loss, stop validation
├── strategy/
│   ├── anchor.py             # Daily anchor capture
│   ├── orders.py             # Order placement / modification
│   ├── trailing.py           # Pure-function trailing logic
│   └── day_runner.py         # Daily orchestration
└── logs/
    └── anchor_bot.log        # Created on first run
```

## Setup (Windows)

### 1. Install Python 3.10+

Download from python.org and ensure "Add to PATH" is ticked.

### 2. Install MT5 terminal

Download from your broker. Log in to your **demo account first** (do not skip this).

### 3. Enable algorithmic trading in MT5

In MT5: **Tools → Options → Expert Advisors → ✓ Allow algorithmic trading**

### 4. Install dependencies

```cmd
cd xauusd_anchor_bot
pip install -r requirements.txt
```

### 5. Configure for your broker

Open `config.py` and edit:

- `SYMBOL` — your broker's gold ticker. Check MT5 Market Watch — it could be `XAUUSD`, `XAUUSDm`, `XAUUSD.r`, `GOLD`, `XAU/USD`, etc.
- `LOT_SIZE` — start small (0.1 on demo to verify behavior, then scale).
- `MAGIC` — keep as-is unless you run multiple bot copies.

## Telegram Setup (Optional)

1. Open Telegram, message **@BotFather**, run `/newbot`, follow prompts. Save the token.
2. Message **@userinfobot** to get your chat ID.
3. Start a chat with your new bot (send any message — required to receive replies).
4. In `config.py`:
   ```python
   TELEGRAM_ENABLED = True
   TELEGRAM_BOT_TOKEN = "1234567890:ABC-DEF..."
   TELEGRAM_CHAT_ID = "987654321"
   ```

If Telegram fails or is disabled, the bot continues normally — alerts are best-effort, not required.

## Running the Bot

```cmd
cd xauusd_anchor_bot
python main.py
```

Leave the MT5 terminal open and the script running. For 24/7 unattended operation, use a Windows VPS.

Stop with **Ctrl+C** — the bot will disconnect cleanly.

## Demo Testing Checklist

**Week 1 — Verify mechanics (do not skip):**

- [ ] Bot connects to MT5 and reports correct account / broker / symbol on startup
- [ ] First day: anchor captured matches the 02:00 M5 candle open on the MT5 chart
- [ ] Both buy stop + sell stop appear in MT5 Trade tab at the correct prices
- [ ] When one stop fires, the other is cancelled within ~5 seconds
- [ ] TP/SL on the open position match config (TP = trigger ± $3, initial SL = trigger ∓ $15)
- [ ] Trailing locks fire and SL moves up step-by-step as price progresses
- [ ] Position closes via either TP or trailed SL — verify in MT5 history
- [ ] On a day with no trigger, pendings are cancelled at 23:00 broker time
- [ ] Logs in `logs/anchor_bot.log` are detailed enough to debug any issue

**Week 2–4 — Statistical validation:**

After ~30 trades on demo, compare your live results against backtest expectations:

| Metric | Backtest |
|---|---|
| Win rate | ~93–95% |
| TP exits | ~33% |
| Lock exits | ~64% |
| SL exits | ~3% |
| Avg net/trade | ~$70 (at 0.5 lot) |

If your demo numbers diverge significantly (especially SL rate > 5%), investigate before going live:
- Wrong symbol?
- Broker spread spikes during 02:00?
- Stops being rejected and ignored?
- Time zone confusion?

## Configuration Reference

| Setting | Default | Description |
|---|---|---|
| `SYMBOL` | `"XAUUSD"` | Broker gold ticker — verify in Market Watch |
| `LOT_SIZE` | `0.5` | Position size |
| `MAGIC` | `20260511` | Order tag — bot only touches its own orders |
| `ANCHOR_HOUR` | `2` | Broker hour for anchor (02:00) |
| `TRIGGER_DIST` | `10.0` | USD from anchor to entry |
| `TP_DIST` | `3.0` | USD from entry to TP |
| `SL_DIST` | `15.0` | USD from entry to SL |
| `LOCK_STEP` | `0.30` | Trail step size |
| `LOCK_STEPS_COUNT` | `9` | Max trail levels |
| `MAX_SPREAD_USD` | `0.50` | Skip if spread above this |
| `MAX_DAILY_LOSS_PCT` | `4.0` | Halt if daily loss exceeds this % |
| `EOD_CANCEL_HOUR` | `23` | When to clean up unfilled pendings |
| `POLL_SECONDS` | `5` | Trail check interval |
| `LOG_LEVEL` | `"INFO"` | DEBUG / INFO / WARNING / ERROR |

## ⚠️ Important Warnings

- **Test on demo first.** No exceptions. The intrabar order assumptions in the strategy backtest may not match live broker execution.
- **Spread spikes can blow stops.** During news, spread can hit $5+. The `MAX_SPREAD_USD` filter only checks at anchor time — once you're in a trade, spread spikes can still cause unexpected fills.
- **Broker stops level.** Some brokers reject SL/TP within X points of market. The bot logs a warning if this might happen, but order placement may still fail. Check `trade_stops_level` in the startup log.
- **VPS recommended for live.** Running on your home PC means missed days when your machine sleeps, restarts, or loses connection.
- **Past performance ≠ future results.** A backtest profitable in Jan–Apr 2026 doesn't guarantee future profit. Trade conservatively.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| "Symbol XAUUSD not found" | Wrong ticker in config — check MT5 Market Watch |
| Orders placed but immediately rejected | Stops too close — check `trade_stops_level` log line |
| Anchor not captured | M5 data not available for that hour — check broker time zone |
| Telegram not sending | Wrong token, chat ID, or you haven't sent the bot a message first |
| Bot stops with error | Check `logs/anchor_bot.log` for full traceback |
