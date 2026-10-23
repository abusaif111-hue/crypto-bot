"""Fixed research and holdout periods (module 4, approved by Sultan).

- Warm-up from 2019-01-01: indicators only, no trades.
- Research (walk-forward) 2020-01-01 to 2025-10-01: covers the 2021 bull,
  2022 bear and 2023 recovery.
- Locked holdout 2025-10-01 to 2026-10-01: the most recent 12 months, run once
  at the end by Sultan. Touching it earlier voids it, so nothing here returns a
  holdout candle; only the holdout command (a later step) can open it.

Changing these dates is a code change that shows up in review, on purpose.
"""
from __future__ import annotations

from datetime import datetime, timezone


def _ms(y: int, m: int, d: int) -> int:
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1000)


WARMUP_START_MS = _ms(2019, 1, 1)
RESEARCH_START_MS = _ms(2020, 1, 1)
HOLDOUT_START_MS = _ms(2025, 10, 1)
HOLDOUT_END_MS = _ms(2026, 10, 1)


HOLDOUT_SPENT = "used by H2 on 2026-10-23"   # every later holdout run is VOID (bot.backtest.holdout)


class HoldoutLocked(PermissionError):
    pass


_TICKET_KEY = object()          # only bot.backtest.holdout.open_holdout uses it (enforced by tests)


class HoldoutTicket:
    """Proof that Sultan opened the holdout with `bot backtest holdout` (typed confirmation,
    recorded in the holdout log). Without one, no code can load or replay a holdout candle."""
    __slots__ = ("operator", "opened_at", "previous_runs")

    def __init__(self, operator: str, opened_at: str, previous_runs: list[str], key: object):
        if key is not _TICKET_KEY:
            raise HoldoutLocked("a holdout ticket can only come from bot backtest holdout")
        self.operator, self.opened_at, self.previous_runs = operator, opened_at, previous_runs

    @property
    def void(self) -> bool:
        """True when the holdout was already used before this run."""
        return bool(self.previous_runs)


def research_only(candles: list) -> list:
    """Candles that finished before the holdout starts. A candle that straddles the
    boundary is dropped too, so no holdout price can leak in."""
    return [c for c in candles if c.close_time < HOLDOUT_START_MS]


def assert_no_holdout(candles: list) -> None:
    for c in candles:
        if c.close_time >= HOLDOUT_START_MS:
            raise HoldoutLocked("holdout data (from 2025-10-01) reached the research code; it stays locked "
                                "until Sultan runs the holdout command")
