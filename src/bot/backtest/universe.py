"""Coin screen for widening the backtest universe (module 4, approved by Sultan, option (b)).

Every rule here was fixed before any new coin's data was looked at.

Candidates: Binance USDT spot pairs trading today, with daily candles back to
2019-10-01 or earlier (so they cover the whole research period: "no new
listings", decision D-16), excluding stablecoins, fiat, gold tokens, wrapped or
staked coins and leveraged tokens. BTC and ETH are always in the universe.

Screen window: 2022-10-01 to 2025-10-01, the 3 years just before the locked
holdout (the holdout is never downloaded or read here). A candidate passes if:
- its 30-day volatility was above 2x BTC's on no more than 25% of days, and
- its largest fall from a peak in the window was no worse than 70%
  (the design package's stability screen), and
- its daily trading volume was at least the risk config's minimum (20 million
  USDT, rule R-11) on at least 95% of days.
Up to 4 passing coins with the highest median daily volume are picked. D-16 allows
up to 8; Sultan chose 4 so the backtest fits the 512 MB server without swap (each coin
adds about 14 MB; decided before any coin's data was seen).

Correlation groups (R-08 as written): a coin whose daily returns correlate with
BTC's at or above `corr_threshold` (0.8) over the window joins BTC and ETH in
"majors"; below that it gets its own group.

Known limit: only coins still listed today can be screened, so coins that were
delisted are missing (survivorship bias).
"""
from __future__ import annotations

import json
import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from bot.analysis.indicators import realized_vol
from bot.backtest import history
from bot.backtest.periods import HOLDOUT_START_MS, WARMUP_START_MS
from bot.core.config import AppConfig
from bot.core.money import ZERO, D
from bot.exchange.filters import SymbolRules
from bot.exchange.interface import Candle


def _ms(y: int, m: int, d: int) -> int:
    return int(datetime(y, m, d, tzinfo=timezone.utc).timestamp() * 1000)


LISTED_BY_MS = _ms(2019, 10, 1)
SCREEN_START_MS = _ms(2022, 10, 1)
SCREEN_END_MS = HOLDOUT_START_MS
VOL_MULTIPLE = Decimal("2")
MAX_SHARE_ABOVE = Decimal("0.25")
MAX_DRAWDOWN = Decimal("0.70")
MIN_LIQUID_SHARE = Decimal("0.95")
MAX_EXTRA_COINS = 4          # D-16 allows 8; 4 fits the server's memory (Sultan's decision)
BASE_MEMORY_MB = 75          # measured: a 2-coin backtest
MEMORY_PER_COIN_MB = 14      # measured: each extra coin's candles and replay data
VOL_DAYS = 30
ALWAYS = ("BTCUSDT", "ETHUSDT")
MAJORS = "majors"

EXCLUDED_BASES = frozenset({
    # stablecoins and fiat
    "USDC", "BUSD", "TUSD", "USDP", "PAX", "DAI", "FDUSD", "USDE", "USD1", "PYUSD", "UST", "USTC", "SUSD",
    "EUR", "EURI", "AEUR", "GBP", "AUD", "TRY", "BRL", "BIDR", "IDRT", "UAH", "RUB", "NGN", "ZAR", "BVND",
    # gold-backed
    "PAXG", "XAUT",
    # wrapped or staked versions of other coins
    "WBTC", "WBETH", "WETH", "BETH", "STETH", "BNSOL", "CBBTC",
})
LEVERAGED_SUFFIXES = ("UP", "DOWN", "BULL", "BEAR")


class SymbolLister(Protocol):
    def all_symbol_rules(self) -> list[SymbolRules]: ...

    def klines(self, symbol: str, interval: str, start_ms: int | None = None, end_ms: int | None = None,
               limit: int = 500) -> list[Candle]: ...

    def symbol_rules(self, symbols: list[str]) -> dict[str, SymbolRules]: ...


def is_candidate(r: SymbolRules) -> bool:
    base = r.base_asset
    if r.quote_asset != "USDT" or not r.is_trading or r.symbol in ALWAYS:
        return False
    if base in EXCLUDED_BASES:
        return False
    if len(base) >= 5 and base.endswith(LEVERAGED_SUFFIXES):
        return False
    return True


@dataclass
class CoinResult:
    symbol: str
    first_candle: str | None = None
    share_vol_above_2x_btc: Decimal | None = None
    max_drawdown: Decimal | None = None
    liquid_share: Decimal | None = None
    median_volume: Decimal | None = None
    corr_with_btc: Decimal | None = None
    failures: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.failures


def _window(cs: list[Candle]) -> list[Candle]:
    return [c for c in cs if SCREEN_START_MS <= c.open_time and c.close_time < SCREEN_END_MS]


def max_drawdown(closes: list[Decimal]) -> Decimal:
    peak, worst = None, ZERO
    for c in closes:
        peak = c if peak is None or c > peak else peak
        worst = max(worst, 1 - c / peak)
    return worst


def correlation(a: list[Decimal], b: list[Decimal]) -> Decimal | None:
    n = len(a)
    if n < 30:
        return None
    ma, mb = sum(a, ZERO) / n, sum(b, ZERO) / n
    cov = sum(((x - ma) * (y - mb) for x, y in zip(a, b)), ZERO)
    va = sum(((x - ma) ** 2 for x in a), ZERO)
    vb = sum(((y - mb) ** 2 for y in b), ZERO)
    return cov / (va.sqrt() * vb.sqrt()) if va > 0 and vb > 0 else None


def assess(symbol: str, daily: list[Candle], btc_daily: list[Candle], cfg: AppConfig) -> CoinResult:
    """Apply the screen to one coin's daily candles (research data only)."""
    res = CoinResult(symbol)
    if not daily:
        res.failures.append("no history")
        return res
    res.first_candle = datetime.fromtimestamp(daily[0].open_time / 1000, timezone.utc).strftime("%Y-%m-%d")
    if daily[0].open_time > LISTED_BY_MS:
        res.failures.append(f"listed after {datetime.fromtimestamp(LISTED_BY_MS / 1000, timezone.utc):%Y-%m-%d}")
        return res
    # 30-day volatility, computed with the 30 days before the window as lead-in
    def vols(cs: list[Candle]) -> dict[int, Decimal]:
        vs = realized_vol([c.close for c in cs], VOL_DAYS)
        return {c.open_time: v for c, v in zip(cs, vs) if v is not None and SCREEN_START_MS <= c.open_time < SCREEN_END_MS}
    coin_v, btc_v = vols(daily), vols(btc_daily)
    days = sorted(set(coin_v) & set(btc_v))
    win = _window(daily)
    if len(days) < 300 or len(win) < 300:
        res.failures.append("not enough data in the screen window")
        return res
    res.share_vol_above_2x_btc = D(sum(1 for d in days if coin_v[d] > VOL_MULTIPLE * btc_v[d])) / len(days)
    res.max_drawdown = max_drawdown([c.close for c in win])
    floor = cfg.risk.min_quote_volume_24h
    res.liquid_share = D(sum(1 for c in win if c.quote_volume >= floor)) / len(win)
    res.median_volume = D(statistics.median([c.quote_volume for c in win]))
    btc_by_day = {c.open_time: c.close for c in _window(btc_daily)}
    pairs = [(c, btc_by_day.get(c.open_time)) for c in win]
    rets_c, rets_b = [], []
    prev = None
    for c, b in pairs:
        if b is not None and prev is not None and prev[1] is not None:
            rets_c.append((c.close / prev[0].close).ln())
            rets_b.append((b / prev[1]).ln())
        prev = (c, b)
    res.corr_with_btc = correlation(rets_c, rets_b)
    if res.share_vol_above_2x_btc > MAX_SHARE_ABOVE:
        res.failures.append(f"volatility above 2x BTC's on {res.share_vol_above_2x_btc * 100:.0f}% of days (max 25%)")
    if res.max_drawdown > MAX_DRAWDOWN:
        res.failures.append(f"worst fall {res.max_drawdown * 100:.0f}% (max 70%)")
    if res.liquid_share < MIN_LIQUID_SHARE:
        res.failures.append(f"volume at least {floor / 1_000_000:.0f}M USDT on only {res.liquid_share * 100:.0f}% "
                            f"of days (need 95%)")
    return res


def pick(results: list[CoinResult]) -> list[CoinResult]:
    ok = [r for r in results if r.passed]
    return sorted(ok, key=lambda r: r.median_volume, reverse=True)[:MAX_EXTRA_COINS]


def groups(picked: list[CoinResult], cfg: AppConfig) -> dict[str, str]:
    out = {s: MAJORS for s in ALWAYS}
    for r in picked:
        corr = r.corr_with_btc
        out[r.symbol] = MAJORS if corr is None or corr >= cfg.risk.corr_threshold else r.symbol
    return out


# ------------------------------------------------------------ the run (downloads through the venue)
def screen(venue: SymbolLister, data_dir: Path, cfg: AppConfig, progress=None) -> dict[str, Any]:
    say = progress or (lambda m: None)
    folder = Path(data_dir) / "screen"
    folder.mkdir(parents=True, exist_ok=True)
    cands = sorted(r.symbol for r in venue.all_symbol_rules() if is_candidate(r))
    say(f"{len(cands)} USDT pairs to check")
    daily: dict[str, list[Candle]] = {}
    for s in ("BTCUSDT", "ETHUSDT"):
        history.fetch(venue, folder, s, "1d", WARMUP_START_MS, SCREEN_END_MS)
        daily[s] = history.load(folder, s, "1d")
    results = []
    for i, s in enumerate(cands, 1):
        first = venue.klines(s, "1d", start_ms=0, limit=1)
        if not first or first[0].open_time > LISTED_BY_MS:
            r = CoinResult(s, first_candle=None if not first else
                           datetime.fromtimestamp(first[0].open_time / 1000, timezone.utc).strftime("%Y-%m-%d"))
            r.failures.append("listed after 2019-10-01" if first else "no history")
            results.append(r)
            continue
        try:
            history.fetch(venue, folder, s, "1d", WARMUP_START_MS, SCREEN_END_MS)
            results.append(assess(s, history.load(folder, s, "1d"), daily["BTCUSDT"], cfg))
        except history.DataError as e:
            r = CoinResult(s)
            r.failures.append(f"bad data: {e}")
            results.append(r)
        if i % 25 == 0:
            say(f"checked {i} of {len(cands)}")
    eth = assess("ETHUSDT", daily["ETHUSDT"], daily["BTCUSDT"], cfg)
    picked = pick(results)
    out = {"screen_window": ["2022-10-01", "2025-10-01"], "listed_by": "2019-10-01",
           "rules": {"max_share_vol_above_2x_btc": str(MAX_SHARE_ABOVE), "max_drawdown": str(MAX_DRAWDOWN),
                     "min_volume_usdt": str(cfg.risk.min_quote_volume_24h), "min_liquid_share": str(MIN_LIQUID_SHARE),
                     "max_extra_coins": MAX_EXTRA_COINS, "corr_threshold": str(cfg.risk.corr_threshold)},
           "symbols": list(ALWAYS) + [r.symbol for r in picked],
           "groups": groups(picked, cfg),
           "eth_reference": _plain(asdict(eth)),
           "results": [_plain({**asdict(r), "passed": r.passed}) for r in results]}
    (folder / "universe.json").write_text(json.dumps(out, indent=1))
    return out


def memory_needed_mb(coins: int) -> int:
    return BASE_MEMORY_MB + MEMORY_PER_COIN_MB * max(0, coins - 2)


def available_memory_mb(meminfo: Path = Path("/proc/meminfo")) -> int | None:
    """MemAvailable from Linux; None where it cannot be read (then no check is made)."""
    try:
        for line in meminfo.read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    except OSError:
        return None
    return None


def load_universe(data_dir: Path) -> dict[str, Any]:
    path = Path(data_dir) / "screen" / "universe.json"
    if not path.exists():
        raise history.DataError(f"no screen result in {path.parent}; run: bot backtest screen")
    return json.loads(path.read_text())


def render(out: dict[str, Any]) -> str:
    passed = [r for r in out["results"] if r["passed"]]
    checked = [r for r in out["results"] if r["share_vol_above_2x_btc"] is not None]
    L = ["# Coin screen", "",
         f"Screen window {out['screen_window'][0]} to {out['screen_window'][1]} (before the locked holdout). "
         f"{len(out['results'])} USDT pairs considered, {len(checked)} with full history, {len(passed)} passed.", "",
         f"**Universe for the H2 re-test: {', '.join(out['symbols'])}**", "",
         "| Coin | Group | Volatility > 2x BTC (days) | Worst fall | Days with enough volume | Median daily volume | "
         "Correlation with BTC | Result |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    rows = sorted(checked, key=lambda r: (not r["passed"], -float(r["median_volume"] or 0)))
    eth = out["eth_reference"]
    for r in [eth] + rows:
        name = r["symbol"] + (" (reference)" if r is eth else "")
        res = "kept" if r is eth else ("PASS" + (" (picked)" if r["symbol"] in out["symbols"] else "")
                                       if r["passed"] else "fail: " + "; ".join(r["failures"]))
        corr = "–" if r["corr_with_btc"] is None else f"{Decimal(r['corr_with_btc']):.2f}"
        L.append(f"| {name} | {out['groups'].get(r['symbol'], '–')} | {_pc(r['share_vol_above_2x_btc'])} | "
                 f"{_pc(r['max_drawdown'])} | {_pc(r['liquid_share'])} | {_m(r['median_volume'])} | {corr} | {res} |")
    late = sum(1 for r in out["results"] if r["share_vol_above_2x_btc"] is None)
    L += ["", f"{late} pairs were left out before the screen (listed after 2019-10-01, no history, or bad data).",
          "Coins delisted before today could not be screened (survivorship bias)."]
    return "\n".join(L) + "\n"


def _pc(x) -> str:
    return "–" if x is None else f"{Decimal(x) * 100:.0f}%"


def _m(x) -> str:
    return "–" if x is None else f"{Decimal(x) / 1_000_000:,.0f}M"


def _plain(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_plain(x) for x in v]
    return v
