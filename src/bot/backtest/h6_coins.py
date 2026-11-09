"""Which coins H6 is tested on: section 2 of docs/h6-plan.md (approved by Sultan on 9 Nov 2026).

H6 trades BTC, ETH, XRP and BNB plus SOL, DOGE, TRX and ZEC. Sultan's decision 1 includes these four
although they failed the design package's stability or liquidity screen (the H5b coin check, 9 Nov 2026);
that screen is therefore not applied here. What still applies, once, before any H6 result:

1. tradeable: a Binance Spot `<COIN>USDT` pair with status TRADING today (exchangeInfo);
2. enough history: 4h candles starting on or before 2021-10-01 (so every test window is covered);
- correlation groups measured over 2022-10-01 to 2025-10-01 as in `bot.backtest.universe` (0.8 or
  more with BTC: "majors"; otherwise its own group);
- memory: if all coins do not fit, the extra coins with the lowest median daily volume are left out one
  at a time until they do; BTC, ETH, XRP and BNB always stay.

The extra coins get a 2.0% stop slippage allowance (plan section 3): the risk engine assumes a worse stop
fill, so it buys less, and the backtest fills their stops 2.0% worse. R-11 (20M USDT daily volume at
order time) is unchanged and still refuses thin days.

Only daily candles up to 2025-10-01 are downloaded here; the spent holdout year is never read. The result
is written once to `h6-coins.json` and never overwritten. `bot backtest run --strategy H6` uses only it.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from bot.backtest import history, universe
from bot.backtest.h5b_coins import BASE, BASE_GROUPS, HISTORY_BY_MS, _day
from bot.backtest.periods import HOLDOUT_START_MS, WARMUP_START_MS
from bot.backtest.universe import MAJORS, CoinResult, SymbolLister, assess, available_memory_mb, memory_needed_mb

DECISION_FILE = "h6-coins.json"
FOLDER = "h6-screen"
INTERVAL = "4h"
MARGIN_MB = 30
EXTRA = ("SOLUSDT", "DOGEUSDT", "TRXUSDT", "ZECUSDT")      # Sultan's decision 1, 9 Nov 2026 (fixed)
EXTRA_STOP_SLIPPAGE_PCT = Decimal("2.0")
# assess() failures that mean "cannot be measured", not the waived screen
_UNMEASURABLE = ("no history", "listed after", "not enough data")


class H6CoinsError(RuntimeError):
    pass


def check_extra(venue: SymbolLister, folder: Path, symbol: str, tradeable: set[str], btc_daily: list,
                cfg) -> CoinResult:
    """Rules 1 and 2, then the measurements for its group and the memory order. The stability and liquidity
    screen is waived (decision 1): its findings are kept for the record as notes, never as failures."""
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
        measured = assess(symbol, history.load(folder, symbol, "1d"), btc_daily, cfg, listed_by=HISTORY_BY_MS)
    except history.DataError as e:
        res.failures.append(f"bad data: {e}")
        return res
    hard = [f for f in measured.failures if f.startswith(_UNMEASURABLE)]
    waived = [f for f in measured.failures if f not in hard]
    measured.failures = hard
    measured.notes = waived                                     # type: ignore[attr-defined]
    return measured


def _group(r: CoinResult, cfg) -> str:
    corr = r.corr_with_btc
    return MAJORS if corr is None or corr >= cfg.risk.corr_threshold else r.symbol


def decide(venue: SymbolLister, data_dir: Path, cfg, operator: str,
           available: Callable[[], int | None] | None = None,
           now: Callable[[], float] = time.time, progress=None) -> dict[str, Any]:
    say = progress or (lambda m: None)
    data_dir = Path(data_dir)
    path = data_dir / DECISION_FILE
    if path.exists():
        raise H6CoinsError(f"H6's coins were already decided ({path}); the check is never repeated")
    if not operator.strip():
        raise H6CoinsError("the check needs an operator name")
    try:
        u = universe.load_universe(data_dir)
    except history.DataError as e:
        raise H6CoinsError(f"H5's coins are not on record: {e}") from None
    if tuple(u["symbols"]) != BASE or any(u["groups"].get(s) != g for s, g in BASE_GROUPS.items()):
        raise H6CoinsError(f"the screen result does not match BTC, ETH, XRP and BNB with their groups "
                           f"({u['symbols']})")
    free = (available or available_memory_mb)()            # looked up at call time (tests replace it)
    if free is None:
        raise H6CoinsError("cannot read the available memory (/proc/meminfo), so nothing can be decided")
    folder = data_dir / FOLDER
    tradeable = {r.symbol for r in venue.all_symbol_rules() if r.quote_asset == "USDT" and r.is_trading}
    history.fetch(venue, folder, "BTCUSDT", "1d", WARMUP_START_MS, universe.SCREEN_END_MS)
    btc_daily = history.load(folder, "BTCUSDT", "1d")
    results = []
    for s in EXTRA:
        say(f"checking {s}")
        results.append(check_extra(venue, folder, s, tradeable, btc_daily, cfg))
    ok = [r for r in results if r.passed]
    kept = sorted(ok, key=lambda r: r.median_volume, reverse=True)
    dropped: list[str] = []
    while kept and memory_needed_mb(len(BASE) + len(kept)) + MARGIN_MB > free:
        dropped.append(kept.pop().symbol)                       # the lowest median volume goes first
    order = {s: i for i, s in enumerate(EXTRA)}
    kept.sort(key=lambda r: order[r.symbol])
    coins = list(BASE) + [r.symbol for r in kept]
    out = {"strategy": "H6", "interval": INTERVAL, "coins": coins, "extra_coins": [r.symbol for r in kept],
           "groups": {**BASE_GROUPS, **{r.symbol: _group(r, cfg) for r in kept}},
           "stop_slippage_pct": {r.symbol: str(EXTRA_STOP_SLIPPAGE_PCT) for r in kept},
           "candidates": list(EXTRA), "passed": [r.symbol for r in ok], "left_out_for_memory": dropped,
           "results": [universe._plain({**asdict(r), "passed": r.passed, "waived": getattr(r, "notes", [])})
                       for r in results],
           "screen_waived": "Sultan's decision 1 (docs/h6-plan.md): the stability and liquidity screen is not applied",
           "history_by": _day(HISTORY_BY_MS), "available_mb": free, "estimate_mb": memory_needed_mb(len(coins)),
           "margin_mb": MARGIN_MB, "operator": operator.strip(),
           "decided_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now()))}
    data_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=1))
    tmp.replace(path)
    return out


def load(data_dir: Path) -> dict[str, Any]:
    path = Path(data_dir) / DECISION_FILE
    if not path.exists():
        raise H6CoinsError("H6's coins are not decided yet; run: bot backtest h6-coins --operator <name>")
    try:
        rec = json.loads(path.read_text())
        assert rec["strategy"] == "H6" and tuple(rec["coins"][:4]) == BASE
        assert rec["coins"][4:] == rec["extra_coins"] and set(rec["extra_coins"]) <= set(EXTRA)
        assert set(rec["groups"]) == set(rec["coins"])
        assert set(rec["stop_slippage_pct"]) == set(rec["extra_coins"])
        assert all(Decimal(v) == EXTRA_STOP_SLIPPAGE_PCT for v in rec["stop_slippage_pct"].values())
        return rec
    except Exception as e:
        raise H6CoinsError(f"{path} is damaged ({type(e).__name__}); it is never rewritten, ask for help") from None


def research_end_ms() -> int:
    """The extra coins' 4h history is downloaded only up to the (spent) holdout's start."""
    return HOLDOUT_START_MS


def backtest_config(base_cfg, rec: dict[str, Any]):
    """The config for H6's backtest: its coins, and the extra coins' 2.0% stop slippage (sizing and fills).
    Only ever larger than the default allowance (it tightens sizing)."""
    risk = base_cfg.risk.model_dump()
    slip = dict(risk["stop_slippage_pct"])
    for s, v in rec["stop_slippage_pct"].items():
        slip[s] = max(Decimal(v), base_cfg.risk.stop_slippage_for(s))
    risk["stop_slippage_pct"] = slip
    return type(base_cfg).model_validate({**base_cfg.model_dump(), "universe": list(rec["coins"]), "risk": risk})


def render(rec: dict[str, Any]) -> str:
    L = [f"H6 coin check ({rec['decided_at']}, {rec['operator']}); stability and liquidity screen waived by "
         f"Sultan's decision 1 (docs/h6-plan.md):", ""]
    for r in rec["results"]:
        if r["passed"]:
            corr = "–" if r["corr_with_btc"] is None else f"{Decimal(r['corr_with_btc']):.2f}"
            fate = ("joins H6, group " + rec["groups"][r["symbol"]] + ", stop slippage 2.0%"
                    if r["symbol"] in rec["extra_coins"] else "left out: memory")
            notes = f"; screen findings, waived: {'; '.join(r['waived'])}" if r["waived"] else ""
            L.append(f"  {r['symbol']:9} OK    ({fate}; correlation with BTC {corr}{notes})")
        else:
            L.append(f"  {r['symbol']:9} out   ({'; '.join(r['failures'])})")
    L += ["", f"Memory available: {rec['available_mb']} MB; estimate for {len(rec['coins'])} coins: "
              f"{rec['estimate_mb']} MB (+ {rec['margin_mb']} MB margin).",
          f"H6 coins: {', '.join(rec['coins'])}"]
    return "\n".join(L) + "\n"
