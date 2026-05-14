"""
Configuration for XAUUSD Anchor Strategy Bot.

All editable settings live here. Edit before running.

SECRETS (Telegram token, chat ID) are read from environment variables —
NEVER commit them to git. See bottom of file for env var names.

Loads from a `.env` file at project root if python-dotenv is installed
(pip install python-dotenv). Falls back to OS env vars otherwise.
"""

import os
from pathlib import Path

# Load .env file if python-dotenv available. Silent no-op if not installed
# or if .env file doesn't exist — env vars can still be set via setx/export.
try:
    from dotenv import load_dotenv
    _ENV_PATH = Path(__file__).resolve().parent / ".env"
    if _ENV_PATH.exists():
        load_dotenv(_ENV_PATH)
except ImportError:
    pass

# ----------------------------------------------------------------------------
# SYMBOL & POSITION
# ----------------------------------------------------------------------------

SYMBOL: str = "XAUUSD"          # Change to match your broker (e.g. "XAUUSDm", "GOLD", "XAUUSD.r")
LOT_SIZE: float = 0.5           # Lot size per trade
MAGIC: int = 20260511           # Bot identifier — only manages its own orders/positions
COMMENT: str = "Anchor02"       # Order comment prefix

# ----------------------------------------------------------------------------
# STRATEGY PARAMETERS (all in USD price units)
# ----------------------------------------------------------------------------

ANCHOR_HOUR: int = 2            # Broker server hour for anchor (02:00)
TRIGGER_DIST: float = 5.0      # Entry: anchor ± 5
TP_DIST: float = 3.0            # Take profit: trigger ± 3
SL_DIST: float = 20.0           # Stop loss: trigger ± 20
LOCK_STEP: float = 0.30         # Trail step size
LOCK_STEPS_COUNT: int = 9       # Number of trail levels (0.30, 0.60, ..., 2.70)

# ----------------------------------------------------------------------------
# SAFETY
# ----------------------------------------------------------------------------

MAX_SPREAD_USD: float = 0.50    # Skip the day if spread above this at anchor time
MAX_DAILY_LOSS_PCT: float = 4.0 # Halt trading if today's bot PnL loss exceeds this %
EOD_CANCEL_HOUR: int = 23       # Cancel unfilled pending orders at this broker hour

# ----------------------------------------------------------------------------
# TIMING
# ----------------------------------------------------------------------------

POLL_SECONDS: int = 5           # How often to poll while managing position
ANCHOR_WAIT_BUFFER: int = 10    # Seconds to wait past anchor_hour:00:00 before reading bar

# ----------------------------------------------------------------------------
# LOGGING
# ----------------------------------------------------------------------------

LOG_FILE: str = "logs/anchor_bot.log"
LOG_LEVEL: str = "INFO"         # DEBUG, INFO, WARNING, ERROR

# ----------------------------------------------------------------------------
# TELEGRAM (optional — leave disabled if not using)
# ----------------------------------------------------------------------------
# Secrets live in environment variables, NOT in this file.
# Set them BEFORE running the bot:
#
#   Windows (cmd, this session only):
#     set TELEGRAM_BOT_TOKEN=1234567890:ABC-DEF...
#     set TELEGRAM_CHAT_ID=1353536439
#     python main.py
#
#   Windows (cmd, persistent across sessions — close + reopen cmd after):
#     setx TELEGRAM_BOT_TOKEN "1234567890:ABC-DEF..."
#     setx TELEGRAM_CHAT_ID "1353536439"
#
#   PowerShell (this session):
#     $env:TELEGRAM_BOT_TOKEN = "1234567890:ABC-DEF..."
#     $env:TELEGRAM_CHAT_ID = "1353536439"
#
#   Linux / macOS / VPS (shell rc file):
#     export TELEGRAM_BOT_TOKEN="1234567890:ABC-DEF..."
#     export TELEGRAM_CHAT_ID="1353536439"
#
# If env vars are not set, telegram silently does nothing (no crash).

TELEGRAM_ENABLED: bool = True
TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.environ.get("TELEGRAM_CHAT_ID", "")

# Position heartbeat — periodic state snapshot while a trade is open.
# Lock/entry/normalize/close telemetry is always sent (when Telegram enabled).
TELEGRAM_POSITION_HEARTBEAT_ENABLED: bool = True
TELEGRAM_POSITION_HEARTBEAT_MINUTES: int = 15

# ----------------------------------------------------------------------------
# ATR FILTER (volatility-based directional bias)
# ----------------------------------------------------------------------------
# Logic at anchor time:
#   - ATR(period) on ATR_TIMEFRAME measured
#   - If ATR > ATR_HIGH_THRESHOLD → SHORT ONLY (high volatility = expect reversal
#     after upward break, so skip LONG)
#   - If ATR < ATR_LOW_THRESHOLD → LONG ONLY (low volatility = expect continuation,
#     skip SHORT)
#   - Else → BOTH (current OCO behavior)
#
# ATR is ALWAYS calculated and logged/telegrammed even when filter is DISABLED.
# This builds up historical ATR observations so you can tune thresholds with data.
#
# Recommended workflow:
#   1. Run with ATR_FILTER_ENABLED=False for 5-10 demo trades
#   2. Note the ATR value at each anchor (telegram + start_log)
#   3. Note which trades won vs. lost vs. their ATR
#   4. Set thresholds based on observed values
#   5. Enable filter (set ATR_FILTER_ENABLED=True)
#
# For XAUUSD M15, typical ATR ranges $1-5. Tune to your broker.

ATR_FILTER_ENABLED: bool = False    # Master switch. Filter only acts when True.
ATR_TIMEFRAME: str = "M15"          # M1, M5, M15, M30, H1, H4
ATR_PERIOD: int = 14
ATR_HIGH_THRESHOLD: float = 5.0     # ATR > this → SHORT only
ATR_LOW_THRESHOLD: float = 1.5      # ATR < this → LONG only

# ----------------------------------------------------------------------------
# WEEKLY PROFIT LOCK ("dam for profits")
# ----------------------------------------------------------------------------
# Once cumulative bot PnL for the CURRENT ISO WEEK reaches the target USD,
# bot stops opening new trades until next Monday (new ISO week resets it).
#
# Backtest result (Jan-Apr 2026, 18 weeks, 68 trades):
#   - Baseline: +$3,465 with 1 losing week (-$480)
#   - With $200 lock: +$3,585 with 0 losing weeks (min week +$45)
#   - Eliminated all weekly drawdowns
#
# How it works: counts pnl from POSITION_CLOSED events in start_log.jsonl
# for the current ISO week. Only realized closed-trade PnL counts; floating
# P&L of open positions is excluded. Resets automatically every Monday.
#
# When lock active: bot still wakes at anchor time, checks lock, sees it's
# hit, logs DAY_SKIPPED reason=weekly_profit_lock, telegrams the user, and
# sleeps until next day. No anchor capture, no orders. Open positions
# continue to be managed normally (lock only blocks NEW trades).
#
# Tune to your situation:
#   - $200-250: backtest sweet spot
#   - $150: more aggressive, locks earlier
#   - $300+: rarely triggers, won't catch SL weeks reliably
#   - 0 or ENABLED=False: disabled, behaves as before

WEEKLY_PROFIT_LOCK_ENABLED: bool = True
WEEKLY_PROFIT_LOCK_USD: float = 250.0   # Backtest optimum. Override at runtime via /setweekly

# ----------------------------------------------------------------------------
# RESCUE HEDGE
# ----------------------------------------------------------------------------
# When original position reaches RESCUE_TRIGGER_ADVERSE (default $10) adverse,
# open an opposite-direction MARKET order to capture continued momentum.
# Both positions then manage independently with same TP/SL/lock-step trail.
#
# Backtest result (Jan-Apr 2026, 68 trades):
#   - Without rescue: +$3,465
#   - With rescue:    +$3,885 (+$420 / +12% improvement)
#   - 8 rescues fired: 6 helped, 2 neutral, 0 hurt
#   - 1 SL day: saved $90
#   - 5 winning days: rescue captured intraday whipsaw profit
#
# REQUIREMENTS
#   - Account MUST be hedging type (not netting). Check MT5: Tools → Options → Trade.
#   - Prop firm must allow same-symbol hedging (most do — FTMO/MFF/FundedNext yes).
#   - Spread cost ~$0.40-$1 per rescue (negligible).
#
# LOT SIZE
#   - RESCUE_LOT_SIZE = None → uses main LOT_SIZE (combined exposure = 2× lot)
#   - RESCUE_LOT_SIZE = 0.25 → half-size rescue (matches single-position risk)
#   - For funded accounts, consider matching exposure to single-position rule

RESCUE_ENABLED: bool = True    # Off by default; flip True after demo validation
RESCUE_TRIGGER_ADVERSE: float = 10.0  # USD adverse from entry that triggers rescue
RESCUE_LOT_SIZE: float = 0.5   # type: ignore  # None → uses LOT_SIZE

# ----------------------------------------------------------------------------
# DUAL-BRACKET MODE (ADVANCED — leave OFF until validated)
# ----------------------------------------------------------------------------
# Default (OFF): standard OCO behavior. When one pending fills, the other
#   is cancelled immediately. Maximum ONE position per day. Max risk = 1×SL.
#
# When ON: BOTH pending orders stay live throughout the day. On whipsaw days
#   where price first runs one direction then reverses through the opposite
#   trigger, both legs can fire and trade independently — each with its own
#   trail state, TP, SL, and close handling. Max risk doubles (= 2×SL) since
#   both could stop out in opposite directions.
#
# REQUIREMENTS BEFORE ENABLING:
#   - Account equity large enough to absorb 2×SL drawdown comfortably
#   - 20+ demo trades validated with single-OCO first
#   - Spread/news filters configured (whipsaw days often coincide with news)
#   - Prop firm lot caps verified to allow combined exposure (2 × DUAL_BRACKET_LOT_SIZE)
#
# DUAL_BRACKET_LOT_SIZE: per-leg lot size when dual mode is enabled.
#   If None, uses LOT_SIZE for each leg (combined exposure = 2 × LOT_SIZE).
#   Common pattern: set to LOT_SIZE / 2 so combined exposure matches single mode.

DUAL_BRACKET_ENABLED: bool = False
DUAL_BRACKET_LOT_SIZE: float = None  # type: ignore   # None → falls back to LOT_SIZE