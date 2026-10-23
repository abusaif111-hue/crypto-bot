"""Price history for backtests (module 4): download, check, cache, load.

Candles come from Binance's public klines through the read-only adapter
(any `SpotReadVenue`), the same data the official archive holds. They are
cached as CSV in a data folder on the server, with the symbol's exchange rules
beside them.

Fail closed: a candle with impossible prices (high below open/close, low above
them, zero or negative values), a duplicate, out-of-order or overlapping time
stops the download. Two things real Binance history has are kept and reported:
- gaps (trading halted), where the backtester sees the next candle's open, so a
  stop passed during a gap fills there, as it would have in reality;
- irregular candles, shorter or longer than the interval, which Binance
  publishes around its maintenance halts (2019-03-12, 2019-05-15 and others).
  The backtester uses each candle's real close time, so nothing is decided
  before a candle has finished.

`load()` never returns a holdout candle (see `bot.backtest.periods`).
"""
from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bot.backtest.periods import HOLDOUT_END_MS, WARMUP_START_MS, HoldoutLocked, HoldoutTicket, research_only
from bot.core.money import D
from bot.exchange.filters import SymbolRules, parse_symbol
from bot.exchange.interface import INTERVAL_MS, KLINES_PAGE as PAGE, Candle, SpotReadVenue

FIELDS = ("open_time", "open", "high", "low", "close", "volume", "close_time", "quote_volume")


class DataError(RuntimeError):
    pass


@dataclass(frozen=True)
class CheckResult:
    count: int
    gaps: tuple[tuple[int, int], ...]        # (first missing time, next candle's open)
    problems: tuple[str, ...]
    irregular: tuple[tuple[int, int], ...] = ()   # (open, close) of candles not exactly one interval long

    @property
    def ok(self) -> bool:
        return not self.problems


def check_candles(candles: list[Candle], interval: str) -> CheckResult:
    step = INTERVAL_MS[interval]
    problems, gaps, irregular = [], [], []
    prev = None
    for c in candles:
        when = c.open_time
        if min(c.open, c.high, c.low, c.close) <= 0:
            problems.append(f"{when}: price not positive")
        if c.high < max(c.open, c.close) or c.low > min(c.open, c.close) or c.low > c.high:
            problems.append(f"{when}: high/low do not contain open/close")
        if c.volume < 0 or c.quote_volume < 0:
            problems.append(f"{when}: negative volume")
        if c.close_time <= c.open_time:
            problems.append(f"{when}: closes before it opens")
        elif c.close_time != c.open_time + step - 1:
            irregular.append((c.open_time, c.close_time))
        if prev is not None:
            if c.open_time <= prev.open_time:
                problems.append(f"{when}: duplicate or out of order")
            elif c.open_time <= prev.close_time:
                problems.append(f"{when}: overlaps the previous candle")
            elif c.open_time != prev.close_time + 1:
                gaps.append((prev.close_time + 1, c.open_time))
        prev = c
    return CheckResult(len(candles), tuple(gaps), tuple(problems), tuple(irregular))


# ------------------------------------------------------------ files
def _csv_path(data_dir: Path, symbol: str, interval: str) -> Path:
    return Path(data_dir) / f"{symbol}-{interval}.csv"


def _rules_path(data_dir: Path, symbol: str) -> Path:
    return Path(data_dir) / f"{symbol}-rules.json"


def read_csv(path: Path) -> list[Candle]:
    out = []
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            out.append(Candle(open_time=int(row["open_time"]), open=D(row["open"]), high=D(row["high"]),
                              low=D(row["low"]), close=D(row["close"]), volume=D(row["volume"]),
                              close_time=int(row["close_time"]), quote_volume=D(row["quote_volume"]), closed=True))
    return out


def write_csv(path: Path, candles: list[Candle]) -> None:
    tmp = Path(str(path) + ".tmp")
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(FIELDS)
        for c in candles:
            w.writerow([c.open_time, c.open, c.high, c.low, c.close, c.volume, c.close_time, c.quote_volume])
    os.replace(tmp, path)


def rules_to_info(r: SymbolRules) -> dict[str, Any]:
    """Back to the exchangeInfo shape, so `parse_symbol` rebuilds the same rules."""
    return {"symbol": r.symbol, "status": r.status, "baseAsset": r.base_asset, "quoteAsset": r.quote_asset,
            "orderTypes": list(r.order_types), "ocoAllowed": r.oco_allowed, "otoAllowed": r.oto_allowed,
            "opoAllowed": r.opo_allowed, "filters": list(r.raw_filters)}


# ------------------------------------------------------------ download
def fetch(venue: SpotReadVenue, data_dir: Path, symbol: str, interval: str = "4h",
          start_ms: int = WARMUP_START_MS, end_ms: int = HOLDOUT_END_MS) -> CheckResult:
    """Download (or extend) the cached history for one symbol. Only finished candles are kept."""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    path = _csv_path(data_dir, symbol, interval)
    have = read_csv(path) if path.exists() else []
    if have and have[0].open_time > start_ms:
        have = []                                     # asked for earlier history: start again
    cursor = have[-1].open_time + INTERVAL_MS[interval] if have else start_ms
    new: list[Candle] = []
    while cursor < end_ms:
        page = venue.klines(symbol, interval, start_ms=cursor, end_ms=end_ms - 1, limit=PAGE)
        page = [c for c in page if c.closed and c.open_time >= cursor]
        if not page:
            break
        new += page
        cursor = page[-1].open_time + INTERVAL_MS[interval]
        if len(page) < PAGE:
            break
    candles = have + new
    result = check_candles(candles, interval)
    if not result.ok:
        raise DataError(f"{symbol} {interval}: bad candles, nothing saved: " + "; ".join(result.problems[:10]))
    write_csv(path, candles)
    rules = venue.symbol_rules([symbol])[symbol]
    _rules_path(data_dir, symbol).write_text(json.dumps(rules_to_info(rules), indent=1))
    return result


# ------------------------------------------------------------ load (research only)
def load(data_dir: Path, symbol: str, interval: str = "4h") -> list[Candle]:
    """Cached candles up to the start of the holdout. Raises DataError if missing or broken."""
    path = _csv_path(Path(data_dir), symbol, interval)
    if not path.exists():
        raise DataError(f"no history for {symbol} {interval} in {data_dir}; run: bot backtest fetch")
    candles = read_csv(path)
    result = check_candles(candles, interval)
    if not result.ok:
        raise DataError(f"{path} is damaged: " + "; ".join(result.problems[:10]))
    return research_only(candles)


def covers(data_dir: Path, symbol: str, interval: str, until_ms: int) -> bool:
    """Whether the cached file reaches `until_ms`. Reads only the last candle's close time, no prices,
    so the holdout can be checked for completeness before it is opened."""
    path = _csv_path(Path(data_dir), symbol, interval)
    if not path.exists():
        return False
    lines = path.read_text().strip().splitlines()
    return len(lines) > 1 and int(lines[-1].split(",")[6]) + 1 >= until_ms


def load_through_holdout(data_dir: Path, symbol: str, interval: str, ticket: HoldoutTicket) -> list[Candle]:
    """Research and holdout candles together (up to 2026-10-01). Only with a holdout ticket."""
    if not isinstance(ticket, HoldoutTicket):
        raise HoldoutLocked("the holdout stays locked: no holdout ticket")
    path = _csv_path(Path(data_dir), symbol, interval)
    if not path.exists():
        raise DataError(f"no history for {symbol} {interval} in {data_dir}; run: bot backtest fetch")
    candles = read_csv(path)
    result = check_candles(candles, interval)
    if not result.ok:
        raise DataError(f"{path} is damaged: " + "; ".join(result.problems[:10]))
    return [c for c in candles if c.close_time < HOLDOUT_END_MS]


def load_rules(data_dir: Path, symbol: str) -> SymbolRules:
    path = _rules_path(Path(data_dir), symbol)
    if not path.exists():
        raise DataError(f"no exchange rules for {symbol} in {data_dir}; run: bot backtest fetch")
    return parse_symbol(json.loads(path.read_text()))


def describe(result: CheckResult, interval: str) -> str:
    hours = sum(b - a for a, b in result.gaps) / 3_600_000
    return (f"{result.count} candles, {len(result.gaps)} gaps ({hours:.1f} hours without candles), "
            f"{len(result.irregular)} irregular-length candles (kept)")
