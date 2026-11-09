"""Which coins H5b is tested on: section 3 of docs/h5b-plan.md (approved by Sultan, 9 Nov 2026).

H5b is H5 (BTC, ETH, XRP, BNB) plus the candidates Sultan named on 9 Nov 2026 that pass every coin
rule. The candidates and rules below were fixed before any of their prices was looked at:

1. tradeable: a Binance Spot `<COIN>USDT` pair with status TRADING today (exchangeInfo);
2. enough history: 4h candles starting on or before 2021-10-01;
3. stable enough and 4. liquid enough: the stability and liquidity screen of `bot.backtest.universe`
   (`assess`), over its screen window 2022-10-01 to 2025-10-01, with rule 2's date as the listing rule.

Every coin that passes joins (no cap, no ranking), with a correlation group measured as in
`bot.backtest.universe` (0.8 or more with BTC: "majors"; otherwise its own). Then the memory check:
if all coins do not fit the memory available, the new coins with the lowest median daily volume are
left out one at a time until they do; BTC, ETH, XRP and BNB always stay.

Only daily candles up to 2025-10-01 are downloaded here; the spent holdout year is never read. The
result is written once to `h5b-coins.json` in the data folder and never overwritten, so the check
cannot be repeated until it gives a wanted answer. `bot backtest run --strategy H5b` uses only the
coins recorded there.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from bot.backtest import history, universe
from bot.backtest.periods import HOLDOUT_START_MS, WARMUP_START_MS
from bot.backtest.universe import MAJORS, CoinResult, SymbolLister, assess, available_memory_mb, memory_needed_mb

DECISION_FILE = "h5b-coins.json"
FOLDER = "h5b-screen"
INTERVAL = "4h"
MARGIN_MB = 30
# H5's coins and correlation groups (as its backtest and H2b's forward test use them)
BASE = ("BTCUSDT", "ETHUSDT", "XRPUSDT", "BNBUSDT")
BASE_GROUPS = {"BTCUSDT": MAJORS, "ETHUSDT": MAJORS, "XRPUSDT": "XRPUSDT", "BNBUSDT": "BNBUSDT"}
# Sultan's list of 9 Nov 2026, besides H5's four (fixed; no coin is added later)
CANDIDATES = ("SOLUSDT", "TRXUSDT", "ZECUSDT", "HYPEUSDT", "DOGEUSDT", "XMRUSDT")
HISTORY_BY_MS = int(datetime(2021, 10, 1, tzinfo=timezone.utc).timestamp() * 1000)
RULES = ("1. a Binance Spot <COIN>USDT pair with status TRADING; "
         "2. 4h candles starting on or before 2021-10-01; "
         "3. volatility above 2x BTC's on at most 25% of days and worst fall at most 70%, and "
         "4. volume at least the R-11 minimum on at least 95% of days, both over 2022-10-01 to 2025-10-01; "
         "groups: correlation with BTC 0.8 or more is 'majors', else its own; "
         "memory: if all do not fit (estimate + 30 MB <= MemAvailable), the new coins with the lowest median "
         "volume are left out one at a time (docs/h5b-plan.md, section 3)")


class H5bCoinsError(RuntimeError):
    pass


def _day(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")


def check_candidate(venue: SymbolLister, folder: Path, symbol: str, tradeable: set[str],
                    btc_daily: list, cfg) -> CoinResult:
    """Rules 1 to 4 for one candidate; stops at the first rule it fails."""
    res = CoinResult(symbol)
    if symbol not in tradeable:
        res.failures.append("not a Binance Spot USDT pair that is trading today (rule 1)")
        return res
    first = venue.klines(symbol, INTERVAL, start_ms=0, limit=1)
    if not first:
        res.failures.append("no 4h history (rule 2)")
        return res
    res.first_candle = _day(first[0].open_time)
    if first[0].open_time > HISTORY_BY_MS:
        res.failures.append(f"4h history starts {res.first_candle}, after 2021-10-01 (rule 2)")
        return res
    try:
        history.fetch(venue, folder, symbol, "1d", WARMUP_START_MS, universe.SCREEN_END_MS)
        daily = history.load(folder, symbol, "1d")
    except history.DataError as e:
        res.failures.append(f"bad data: {e}")
        return res
    return assess(symbol, daily, btc_daily, cfg, listed_by=HISTORY_BY_MS)


def _group(r: CoinResult, cfg) -> str:
    corr = r.corr_with_btc
    return MAJORS if corr is None or corr >= cfg.risk.corr_threshold else r.symbol


def decide(venue: SymbolLister, data_dir: Path, cfg, operator: str,
           available: Callable[[], int | None] = available_memory_mb,
           now: Callable[[], float] = time.time, progress=None) -> dict[str, Any]:
    say = progress or (lambda m: None)
    data_dir = Path(data_dir)
    path = data_dir / DECISION_FILE
    if path.exists():
        raise H5bCoinsError(f"H5b's coins were already decided ({path}); the check is never repeated")
    if not operator.strip():
        raise H5bCoinsError("the check needs an operator name")
    try:
        u = universe.load_universe(data_dir)
    except history.DataError as e:
        raise H5bCoinsError(f"H5's coins are not on record: {e}") from None
    if tuple(u["symbols"]) != BASE or any(u["groups"].get(s) != g for s, g in BASE_GROUPS.items()):
        raise H5bCoinsError(f"the screen result does not match H5's coins and groups ({u['symbols']}); "
                            f"H5b starts from exactly {', '.join(BASE)}")
    free = available()
    if free is None:
        raise H5bCoinsError("cannot read the available memory (/proc/meminfo), so nothing can be decided")
    folder = data_dir / FOLDER
    tradeable = {r.symbol for r in venue.all_symbol_rules() if r.quote_asset == "USDT" and r.is_trading}
    history.fetch(venue, folder, "BTCUSDT", "1d", WARMUP_START_MS, universe.SCREEN_END_MS)
    btc_daily = history.load(folder, "BTCUSDT", "1d")
    results = []
    for s in CANDIDATES:
        say(f"checking {s}")
        results.append(check_candidate(venue, folder, s, tradeable, btc_daily, cfg))
    passed = [r for r in results if r.passed]
    kept = sorted(passed, key=lambda r: r.median_volume, reverse=True)
    dropped: list[str] = []
    while kept and memory_needed_mb(len(BASE) + len(kept)) + MARGIN_MB > free:
        dropped.append(kept.pop().symbol)                       # the lowest median volume goes first
    order = {s: i for i, s in enumerate(CANDIDATES)}
    kept.sort(key=lambda r: order[r.symbol])
    coins = list(BASE) + [r.symbol for r in kept]
    groups = {**BASE_GROUPS, **{r.symbol: _group(r, cfg) for r in kept}}
    out = {"strategy": "H5b", "interval": INTERVAL, "coins": coins, "new_coins": [r.symbol for r in kept],
           "groups": groups, "candidates": list(CANDIDATES), "passed": [r.symbol for r in passed],
           "left_out_for_memory": dropped,
           "results": [universe._plain({**asdict(r), "passed": r.passed}) for r in results],
           "screen_window": ["2022-10-01", "2025-10-01"], "history_by": _day(HISTORY_BY_MS),
           "available_mb": free, "estimate_mb": memory_needed_mb(len(coins)), "margin_mb": MARGIN_MB,
           "rules": RULES, "operator": operator.strip(),
           "decided_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now()))}
    data_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=1))
    tmp.replace(path)
    return out


def load(data_dir: Path) -> dict[str, Any]:
    path = Path(data_dir) / DECISION_FILE
    if not path.exists():
        raise H5bCoinsError("H5b's coins are not decided yet; run: bot backtest h5b-coins --operator <name>")
    try:
        rec = json.loads(path.read_text())
        assert rec["strategy"] == "H5b" and tuple(rec["coins"][:4]) == BASE
        assert rec["coins"][4:] == rec["new_coins"] and set(rec["new_coins"]) <= set(CANDIDATES)
        assert set(rec["groups"]) == set(rec["coins"])
        return rec
    except Exception as e:
        raise H5bCoinsError(f"{path} is damaged ({type(e).__name__}); it is never rewritten, ask for help") from None


def research_end_ms() -> int:
    """New coins' 4h history is downloaded only up to the (spent) holdout's start."""
    return HOLDOUT_START_MS


def render(rec: dict[str, Any]) -> str:
    L = [f"H5b coin check ({rec['decided_at']}, {rec['operator']}), rules fixed in docs/h5b-plan.md:", ""]
    for r in rec["results"]:
        if r["passed"]:
            corr = "–" if r["corr_with_btc"] is None else f"{Decimal(r['corr_with_btc']):.2f}"
            fate = ("joins H5b, group " + rec["groups"][r["symbol"]] if r["symbol"] in rec["new_coins"]
                    else "passed, but left out: memory")
            L.append(f"  {r['symbol']:9} PASS  ({fate}; correlation with BTC {corr}; volatility above 2x BTC on "
                     f"{Decimal(r['share_vol_above_2x_btc']) * 100:.0f}% of days, worst fall "
                     f"{Decimal(r['max_drawdown']) * 100:.0f}%, enough volume on {Decimal(r['liquid_share']) * 100:.0f}% "
                     f"of days)")
        else:
            L.append(f"  {r['symbol']:9} fail  ({'; '.join(r['failures'])})")
    L += ["", f"Memory available: {rec['available_mb']} MB; estimate for {len(rec['coins'])} coins: "
              f"{rec['estimate_mb']} MB (+ {rec['margin_mb']} MB margin)."]
    if not rec["new_coins"]:
        L.append("No candidate joins: H5b is dropped without a backtest (plan section 3). H5 carries on unchanged.")
    else:
        L.append(f"H5b coins: {', '.join(rec['coins'])}")
    return "\n".join(L) + "\n"
