"""Custom exceptions for clean halt propagation."""


class HaltBot(Exception):
    """
    Raised when the bot detects a critical / ambiguous state requiring
    manual review. Should propagate up to main() and exit the bot cleanly.

    Examples of conditions warranting HaltBot:
      - Multiple bot positions detected at runtime (shouldn't happen)
      - Cancel of survivor pending failed during partial OCO
      - Startup MT5 query failed during recovery checks (state unknown)
    """
    pass
