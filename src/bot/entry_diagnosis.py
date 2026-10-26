"""Why H2b is not entering trades (read-only diagnosis; asked for by Sultan, 26 Oct 2026).

For every account (live, testnet, the H2b forward test, paper) and every coin, each daily decision
since the account started is put in exactly one category:

1. NO_SIGNAL   H2b's own `signal()` said no (the reason is H2b's: lookbacks, volatility pause, history).
2. MIN_ORDER   H2b signalled, but the risk engine refused it because the order would be below Binance's
               minimum within the 0.25% risk budget (REJ_BELOW_EXCHANGE_MIN, or "too small to protect").
3. NOT_FILLED  an entry order was sent but not filled (fill-or-kill expired, or the simulator did not fill).
4. BLOCKED     the account was paused or blocked (mode, breaker, reconciliation), or the runner missed
               the daily decision (down, failing, or no journal line in the decision window).
5. OTHER       any other reason (another risk-engine code, refused by Binance, skipped, unknown outcome,
               no valid stop, or a signal the journal does not explain).
and ENTERED    the trade was opened.

The signal is recomputed with `BlendedMomentum.signal()` itself on the finished candles the runner saw
(those closed before 00:00 UTC of the decision day), so it cannot disagree with H2b. What the runner did
comes from its own journal. For every day H2b signalled, the diagnosis also asks the risk engine,
`evaluate()` itself, whether that idea could meet Binance's minimum order at the account's capital on a
fresh account with a good market, and how much capital it would have needed: the evidence for whether
valid signals are being rejected.

Read-only: it reads the runners' files and public Binance data (exchange rules and candles, no key). It
imports nothing that trades, writes no file, never changes H2b, its settings, its capital or its forward
test. Days before the end of the spent holdout year (1 Oct 2026) are never measured; candles from that
year are only indicator history for later days, as in the forward report.
"""
from __future__ import annotations

import json
import time
from collections import Counter
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

from bot.analysis.resample import DAY_MS
from bot.backtest.periods import HOLDOUT_END_MS
from bot.core.config import AppConfig
from bot.core.money import D, plain
from bot.exchange.filters import SymbolRules
from bot.exchange.interface import Candle
from bot.risk.engine import evaluate
from bot.risk.models import AccountState, MarketSnapshot, Mode, RejectCode
from bot.strategies.base import entry_limit, make_intent
from bot.strategies.h2b_blend import BlendedMomentum

NO_SIGNAL, MIN_ORDER, NOT_FILLED, BLOCKED, OTHER, ENTERED = (
    "no_signal", "min_order", "not_filled", "blocked", "other", "entered")
CATEGORIES = (NO_SIGNAL, MIN_ORDER, NOT_FILLED, BLOCKED, OTHER, ENTERED)
LABELS = {
    NO_SIGNAL: "1 no qualifying H2b signal",
    MIN_ORDER: "2 signal, below Binance's minimum order within the risk budget",
    NOT_FILLED: "3 order sent, not filled",
    BLOCKED: "4 paused, blocked, reconciliation, or the daily decision missed",
    OTHER: "5 another reason",
    ENTERED: "entered",
}
ACCOUNTS = (("live", "live"), ("testnet", "testnet"), ("forward", "forward-h2b"), ("paper", "paper"))
JOURNAL, ACCOUNT, STATE, FORWARD = "journal.jsonl", "account.json", "state.json", "forward.json"
WINDOW_MS = 4 * 3_600_000             # H2b decides at the first 4h close after 00:00 UTC
ENTRY_EVENTS = {"entry_protected", "entry_filled", "entry_not_filled", "entry_rejected", "entry_refused",
                "entry_blocked", "entry_skipped", "entry_unknown", "entry_emergency_exit", "entry_unprotected"}
TOO_SMALL = "too small to protect"    # bot.exchange.trading's own words for a protection below the minimum
PROTECT_MARGIN = Decimal("1.02")       # as ProtectedEntryRequest.problems (a test keeps the two equal)
FEE = Decimal("0.001")
CAPITAL_SEARCH_MAX = Decimal("100000")
STOP_PCTS = ("3", "4.5", "6", "9", "12", "15")
SHOW_SIGNALS = 40


# ---------------------------------------------------------------- reading the runners' files
@dataclass
class AccountInfo:
    name: str
    folder: Path
    found: bool = False
    started_at: float | None = None
    capital: Decimal | None = None          # what the risk engine sizes on (live: the capped capital)
    mode: str = "?"
    breaches: list[str] = field(default_factory=list)
    last_change: dict[str, Any] | None = None
    last_line_at: float | None = None
    problems: list[str] = field(default_factory=list)


def _read_json(path: Path, info: AccountInfo) -> Any:
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        info.problems.append(f"{path.name} is unreadable ({type(e).__name__})")
        return None


def read_journal(path: Path, since_ms: int | None = None) -> list[dict[str, Any]]:
    """The whole journal, line by line (a bad line is skipped). Only lines at or after `since_ms`."""
    out: list[dict[str, Any]] = []
    if not path.exists():
        return out
    with open(path, "rb") as f:
        for raw in f:
            try:
                e = json.loads(raw)
                at = float(e.get("at"))
            except (ValueError, TypeError, AttributeError):
                continue
            if since_ms is None or at * 1000 >= since_ms:
                out.append(e)
    return out


def read_account(home: Path, name: str, folder: str) -> AccountInfo:
    d = Path(home) / folder
    info = AccountInfo(name, d, found=d.is_dir())
    if not info.found:
        return info
    acct = _read_json(d / ACCOUNT, info)
    state = _read_json(d / STATE, info) if not isinstance(acct, dict) else None
    saved = acct if isinstance(acct, dict) else state if isinstance(state, dict) else {}
    if isinstance(acct, dict):
        info.started_at = _f(acct.get("started_at"))
        info.capital = _d(acct.get("capital"))
    elif isinstance(state, dict):
        info.started_at = _f(state.get("created_at"))
        info.capital = _d(state.get("starting_quote"))
    fwd = _read_json(d / FORWARD, info)
    if isinstance(fwd, dict) and _f(fwd.get("started_at")) is not None:
        info.started_at = _f(fwd.get("started_at"))
    modes = saved.get("modes") or {}
    info.mode = str(modes.get("mode") or "?")
    info.breaches = list(modes.get("active_breaches") or [])
    hist = modes.get("history") or []
    if hist and isinstance(hist[-1], dict):
        info.last_change = {k: hist[-1].get(k) for k in ("at", "from_mode", "to_mode", "actor", "reason")}
    return info


def _f(v: Any) -> float | None:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _d(v: Any) -> Decimal | None:
    try:
        return None if v is None else D(v)
    except Exception:
        return None


# ---------------------------------------------------------------- H2b's verdict on one day
@dataclass(frozen=True)
class DaySignal:
    symbol: str
    day_ms: int                    # 00:00 UTC of the decision day
    enter: bool
    why: str                       # H2b's own reason when it would not buy
    entry: Decimal | None = None
    stop: Decimal | None = None
    stop_pct: Decimal | None = None
    min_order_ok: bool | None = None       # could the idea meet Binance's minimum at the account's capital?
    engine_code: str | None = None         # the risk engine's code on a fresh account (None: accepted)
    capital_needed: Decimal | None = None  # the smallest capital the risk engine would accept for it


def day_signal(strategy: BlendedMomentum, candles: list[Candle], symbol: str, day_ms: int) -> DaySignal:
    """H2b's decision at 00:00 UTC of `day_ms`, from the candles that had closed by then."""
    seen = [c for c in candles if c.close_time < day_ms][-strategy.history_needed():]   # what the runner fetches
    days = strategy._daily(seen)
    enter, why = strategy.signal(days)
    if not enter:
        return DaySignal(symbol, day_ms, False, why)
    entry = entry_limit(seen[-1].close)
    plan = strategy.exit_plan(seen, entry)
    if plan is None or make_intent(symbol, entry, plan[0], strategy.name, plan[1], "x") is None:
        return DaySignal(symbol, day_ms, True, "no valid stop (daily ATR)", entry)
    return DaySignal(symbol, day_ms, True, "", entry, plan[0], (entry - plan[0]) / entry * 100)


def protected_ok(qty: Decimal, stop: Decimal, rules: SymbolRules, fee: Decimal = FEE) -> bool:
    """bot.exchange.trading's check that the protective orders can be placed (a test keeps them equal)."""
    protected = qty * (1 - 2 * fee)
    return protected * stop >= rules.min_notional * PROTECT_MARGIN and protected >= rules.min_qty


def engine_check(sig: DaySignal, capital: Decimal, rules: SymbolRules, cfg: AppConfig, fee: Decimal = FEE
                 ) -> tuple[bool, str | None]:
    """The risk engine's answer for this idea on a fresh account of `capital` with a good market, plus the
    protection check. (accepted, code); 'too small to protect' is reported as the minimum-order code."""
    intent = make_intent(sig.symbol, sig.entry, sig.stop, "H2b", 720, "2 of the 14/28/56-day returns turn negative")
    ask = sig.entry / Decimal("1.001")
    state = AccountState(mode=Mode.RUNNING, equity=capital, free_quote=capital, day_start_equity=capital,
                         week_start_equity=capital, daily_pnl=D(0), weekly_pnl=D(0), drawdown_pct=D(0),
                         positions=(), taker_fee_rate=fee)
    market = MarketSnapshot(best_bid=ask * Decimal("0.9999"), best_ask=ask, quote_volume_24h=D("1e12"),
                            ask_depth_quote=D("1e12"), data_age_s=D(0), clock_skew_ms=0, reference_price=ask)
    d = evaluate(intent, state, market, rules, cfg, "diagnosis")
    if not d.accepted:
        return False, d.code.value if d.code else "rejected"
    if not protected_ok(d.qty, sig.stop, rules, fee):
        return False, RejectCode.EXCHANGE_MIN.value
    return True, None


def capital_needed(sig: DaySignal, rules: SymbolRules, cfg: AppConfig) -> Decimal | None:
    """The smallest capital (to 1 USDT) at which the idea meets the minimum order, risk settings unchanged."""
    if engine_check(sig, CAPITAL_SEARCH_MAX, rules, cfg)[0] is False:
        return None
    lo, hi = Decimal(1), CAPITAL_SEARCH_MAX
    while hi - lo > 1:
        mid = ((lo + hi) / 2).to_integral_value()
        if engine_check(sig, mid, rules, cfg)[0]:
            hi = mid
        else:
            lo = mid
    return hi


def with_min_order(sig: DaySignal, capital: Decimal | None, rules: SymbolRules | None, cfg: AppConfig) -> DaySignal:
    if not sig.enter or sig.stop is None or rules is None or capital is None or capital <= 0:
        return sig
    ok, code = engine_check(sig, capital, rules, cfg)
    return DaySignal(sig.symbol, sig.day_ms, sig.enter, sig.why, sig.entry, sig.stop, sig.stop_pct, ok, code,
                     capital_needed(sig, rules, cfg))


# ---------------------------------------------------------------- one coin-day, one category
@dataclass(frozen=True)
class Decision:
    account: str
    symbol: str
    day_ms: int
    category: str
    reason: str
    signal: DaySignal


def classify(sig: DaySignal, events: list[dict[str, Any]]) -> tuple[str, str]:
    """`events`: this account's journal lines in the decision day (all of them)."""
    mine = [e for e in events if e.get("event") in ENTRY_EVENTS and e.get("symbol") == sig.symbol
            and str(e.get("strategy") or "H2b") == "H2b"]
    if mine:
        return _from_journal(mine[0], sig)
    if not sig.enter:
        return NO_SIGNAL, sig.why
    if sig.why:                                                   # H2b proposes nothing without a valid stop
        return OTHER, sig.why
    window = [e for e in events if sig.day_ms <= float(e.get("at", 0)) * 1000 < sig.day_ms + WINDOW_MS]
    trouble = [e for e in window if e.get("event") == "strategy_error"]
    if trouble:
        return BLOCKED, f"daily decision missed: strategy_error {str(trouble[0].get('reason') or '')[:80]}"
    if not window:
        return BLOCKED, "daily decision missed: the runner wrote nothing in the 00:00-04:00 UTC window (down?)"
    return OTHER, "H2b signalled but the journal shows no idea for this coin (check the runner's log)"


def _from_journal(e: dict[str, Any], sig: DaySignal) -> tuple[str, str]:
    ev, code, reason = e.get("event"), str(e.get("code") or ""), str(e.get("reason") or "")
    if ev in ("entry_protected", "entry_filled"):
        return ENTERED, "opened"
    if ev == "entry_not_filled":
        return NOT_FILLED, reason[:120] or "not filled"
    if ev == "entry_blocked":
        why = e.get("reason") or f"mode {e.get('mode')}" + (f", breaches {e.get('breaches')}" if e.get("breaches") else "")
        return BLOCKED, str(why)[:160]
    if (ev == "entry_rejected" and code == RejectCode.EXCHANGE_MIN.value) or TOO_SMALL in reason:
        return MIN_ORDER, reason[:160] or code
    if ev == "entry_rejected" and code == RejectCode.MODE.value:
        return BLOCKED, reason[:160] or code
    if not sig.enter:
        return OTHER, f"{ev} {code} {reason}"[:160] + " (the journal has an idea H2b's recomputed signal does not)"
    return OTHER, f"{ev} {code} {reason}".strip()[:160]


# ---------------------------------------------------------------- everything
@dataclass
class Diagnosis:
    now: float
    accounts: list[AccountInfo]
    rules: dict[str, SymbolRules]
    decisions: list[Decision]
    last_close: dict[str, Decimal]                  # per coin, for the minimum-capital table
    first_day_ms: dict[str, int]
    errors: list[str] = field(default_factory=list)


def decision_days(start_s: float | None, now: float, since_ms: int | None = None) -> list[int]:
    """00:00 UTC of every decision day after the start, never before the spent holdout year ended."""
    if start_s is None:
        return []
    first = max(int(start_s * 1000), since_ms or 0, HOLDOUT_END_MS)
    day = -(-first // DAY_MS) * DAY_MS                             # the first midnight at or after `first`
    out = []
    while day <= now * 1000:
        out.append(day)
        day += DAY_MS
    return out


def diagnose(home: Path, strategy: BlendedMomentum, cfg: AppConfig, candles: dict[str, list[Candle]],
             rules: dict[str, SymbolRules], now: float, since_ms: int | None = None,
             names: Iterable[str] | None = None) -> Diagnosis:
    accounts = [read_account(home, n, f) for n, f in ACCOUNTS if names is None or n in names]
    out = Diagnosis(now, accounts, rules, [], {s: c[-1].close for s, c in candles.items() if c}, {})
    cache: dict[tuple[str, int], DaySignal] = {}
    for a in accounts:
        if not a.found:
            continue
        days = decision_days(a.started_at, now, since_ms)
        if not days:
            continue
        out.first_day_ms[a.name] = days[0]
        events = read_journal(a.folder / JOURNAL, days[0])
        if events:
            a.last_line_at = float(events[-1]["at"])
        by_day: dict[int, list[dict[str, Any]]] = {}
        for e in events:
            by_day.setdefault(int(float(e["at"]) * 1000) // DAY_MS * DAY_MS, []).append(e)
        for s in strategy.symbols:
            if s not in candles:
                continue
            for day in days:
                if (s, day) not in cache:
                    cache[(s, day)] = day_signal(strategy, candles[s], s, day)
                sig = with_min_order(cache[(s, day)], a.capital, rules.get(s), cfg)
                cat, why = classify(sig, by_day.get(day, []))
                out.decisions.append(Decision(a.name, s, day, cat, why, sig))
    return out


def counts(decisions: list[Decision]) -> dict[str, Counter]:
    """Per coin (and 'all'): how many coin-days fell in each category."""
    out: dict[str, Counter] = {"all": Counter()}
    for d in decisions:
        out.setdefault(d.symbol, Counter())[d.category] += 1
        out["all"][d.category] += 1
    return out


# ---------------------------------------------------------------- the text
def _day(ms: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ms / 1000))


def _when(t: Any) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(float(t)))
    except (TypeError, ValueError):
        return str(t) if t else "?"


def rules_lines(rules: dict[str, SymbolRules], last_close: dict[str, Decimal], cfg: AppConfig) -> list[str]:
    out = ["Binance live rules (public exchangeInfo, read now):"]
    for s, r in sorted(rules.items()):
        mkt = (f"; market step {plain(r.market_step_size)}, market min {plain(r.market_min_qty)}"
               if r.market_step_size else "")
        out.append(f"  {s:<9} {r.status}; min order {plain(r.min_notional)} USDT"
                   f"{' (market orders too)' if r.notional_applies_to_market else ''}; step {plain(r.step_size)}, "
                   f"min qty {plain(r.min_qty)}{mkt}; tick {plain(r.tick_size)}; "
                   f"STOP_LOSS {'yes' if 'STOP_LOSS' in r.order_types else 'NO'}, "
                   f"OPO/OTO/OCO {'yes' if r.opo_allowed and r.oto_allowed and r.oco_allowed else 'NO'}")
    risk = cfg.risk.risk_per_trade_pct
    out.append(f"Smallest capital the risk engine accepts at {plain(risk)}% risk per trade, for a stop this far below "
               f"the entry (risk settings unchanged; fresh account, good market):")
    for s, r in sorted(rules.items()):
        cells = []
        entry = entry_limit(last_close[s]) if s in last_close else None
        if entry is None:
            continue
        for pct_ in STOP_PCTS:
            stop = entry * (1 - D(pct_) / 100)
            probe = DaySignal(s, 0, True, "", entry, stop, D(pct_))
            need = capital_needed(probe, r, cfg)
            cells.append(f"{pct_}%: {'-' if need is None else plain(need)}")
        out.append(f"  {s:<9} " + "  ".join(cells))
    return out


def render(dx: Diagnosis, cfg: AppConfig) -> str:
    L = [f"Why H2b is not entering: read-only diagnosis, {_when(dx.now)}. Changes nothing.",
         "Each decision day per coin is in one category. The signal is H2b's own signal() on the candles the runner "
         "saw; what the runner did is its journal. Days before 2026-10-01 are never measured."]
    L += [f"ERROR: {e}" for e in dx.errors]
    L.append("")
    L += rules_lines(dx.rules, dx.last_close, cfg)
    for a in dx.accounts:
        L.append("")
        if not a.found:
            L.append(f"{a.name.upper()}: no folder at {a.folder}")
            continue
        L.append(f"{a.name.upper()} ({a.folder}): mode {a.mode}"
                 + (f", active {', '.join(a.breaches)}" if a.breaches else ", nothing active")
                 + (f"; sizes on {plain(a.capital)} USDT" if a.capital is not None else "")
                 + (f"; started {_when(a.started_at)}" if a.started_at else ""))
        if a.last_line_at is not None:
            mins = int(max(0, dx.now - a.last_line_at) // 60)
            L.append(f"  last journal line {_when(a.last_line_at)} ({mins} min ago)"
                     + ("; THE RUNNER MAY BE DOWN" if mins > 40 else ""))
        if a.last_change:
            c = a.last_change
            L.append(f"  last mode change: {c.get('from_mode')} -> {c.get('to_mode')} by {c.get('actor')} at "
                     f"{str(c.get('at'))[:16]}: {str(c.get('reason') or '')[:120]}")
        if a.mode != Mode.RUNNING.value:
            L.append(f"  ENTRIES ARE BLOCKED NOW (mode {a.mode}). Nothing can enter until an operator resumes"
                     + (" and the active items clear." if a.breaches else "."))
        L += [f"  problem: {p}" for p in a.problems]
        mine = [d for d in dx.decisions if d.account == a.name]
        if not mine:
            L.append("  no decision day to measure yet (it starts at the first 00:00 UTC after the start, and never "
                     "before 2026-10-01)")
            continue
        days = sorted({d.day_ms for d in mine})
        L.append(f"  {len(days)} decision days ({_day(days[0])} to {_day(days[-1])}) x {len({d.symbol for d in mine})} "
                 f"coins = {len(mine)} coin-days")
        L += _table(mine)
        sigs = [d for d in mine if d.signal.enter and not d.signal.why]
        if sigs:
            bad = [d for d in sigs if d.signal.min_order_ok is False]
            cap = "?" if a.capital is None else plain(a.capital)
            L.append(f"  H2b signalled on {len(sigs)} coin-days. At {cap} USDT, {len(bad)} of them could not "
                     f"meet Binance's minimum order within the risk budget, whatever else happened:")
            for d in sigs[:SHOW_SIGNALS]:
                s = d.signal
                fit = ("fits the minimum" if s.min_order_ok else
                       f"BELOW THE MINIMUM ({s.engine_code})" if s.min_order_ok is False else "not checked")
                need = f"; needs at least {plain(s.capital_needed)} USDT" if s.capital_needed is not None else ""
                L.append(f"    {_day(d.day_ms)} {d.symbol:<9} stop {s.stop_pct:.1f}% below the entry: {fit}{need}; "
                         f"outcome: {LABELS[d.category]} ({d.reason[:90]})")
            if len(sigs) > SHOW_SIGNALS:
                L.append(f"    ... and {len(sigs) - SHOW_SIGNALS} more signal days")
        else:
            L.append("  H2b signalled on no coin-day in this period.")
        odd = [d for d in mine if not d.signal.enter and d.category != NO_SIGNAL]
        L.append(f"  check: the journal had an H2b idea on {len(odd)} coin-days where the recomputed signal said no"
                 + (" (these should be 0; look at them)" if odd else " (as it should be)"))
        why = Counter(d.reason for d in mine if d.category == NO_SIGNAL)
        if why:
            L.append("  why there was no signal (H2b's own words): "
                     + "; ".join(f"{r} x{n}" for r, n in why.most_common(6)))
    L += ["", "Notes: 'below the minimum' is the risk engine's answer on a fresh account with a good market, so it "
          "isolates the minimum-order limit from the other checks. The live account sizes on at most its capital."]
    return "\n".join(L) + "\n"


def _table(mine: list[Decision]) -> list[str]:
    c = counts(mine)
    coins = sorted(k for k in c if k != "all") + ["all"]
    head = f"  {'category':<62}" + "".join(f"{k.replace('USDT', ''):>12}" for k in coins)
    rows = [head]
    for cat in CATEGORIES:
        cells = ""
        for k in coins:
            n, tot = c[k][cat], sum(c[k].values())
            cells += f"{n:>5} {n / tot * 100:>5.1f}%" if tot else f"{'-':>12}"
        rows.append(f"  {LABELS[cat]:<62}{cells}")
    return rows
