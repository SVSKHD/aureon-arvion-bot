"""
Configuration for XAUUSD Anchor Strategy Bot.

All editable settings live here. Edit before running.
"""

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
TRIGGER_DIST: float = 10.0      # Entry: anchor ± 10
TP_DIST: float = 3.0            # Take profit: trigger ± 3
SL_DIST: float = 15.0           # Stop loss: trigger ± 15
LOCK_STEP: float = 0.30         # Trail step size
LOCK_STEPS_COUNT: int = 9       # Number of trail levels (0.30, 0.60, ..., 2.70)

# ----------------------------------------------------------------------------
# ATR FILTER (volatility-based directional bias)
# ----------------------------------------------------------------------------
ATR_FILTER_ENABLED: bool = False
ATR_TIMEFRAME: str = "M15"
ATR_PERIOD: int = 14
ATR_HIGH_THRESHOLD: float = 5.0
ATR_LOW_THRESHOLD: float = 1.5

# ----------------------------------------------------------------------------
# SAFETY
# ----------------------------------------------------------------------------
MAX_SPREAD_USD: float = 0.50
MAX_DAILY_LOSS_PCT: float = 4.0
EOD_CANCEL_HOUR: int = 23      # Cancel unfilled pending orders at this broker hour

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

TELEGRAM_ENABLED: bool = True
TELEGRAM_BOT_TOKEN: str = "8602784661:AAFt3sV48c7Uj-gbtEWgrdfYFQyqQrhzvic"    # From @BotFather (e.g. "1234567890:ABC-DEF...")
TELEGRAM_CHAT_ID: str = "1353536439"      # Your chat ID (find with @userinfobot)

# Position heartbeat — periodic state snapshot while a trade is open.
# Lock/entry/normalize/close telemetry is always sent (when Telegram enabled).
TELEGRAM_POSITION_HEARTBEAT_ENABLED: bool = True
TELEGRAM_POSITION_HEARTBEAT_MINUTES: int = 15
