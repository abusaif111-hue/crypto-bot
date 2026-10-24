"""Risk monitor (docs/intel-plan.md, section 7; approved by Sultan on 24 Oct 2026).

One read-only check across every account the bot runs (live, testnet, the H2b forward test, paper)
and the coins they trade. Each finding has a level (info, warning, critical), the rule, the reason in
words and the numbers it used, and is appended to `~/monitor/findings.jsonl`.

Read-only by design: it reads the runners' own files (as the dashboard does) and public Binance prices
through the read-only market data service. It imports nothing that trades, never writes into a runner's
folder, never reads `.env`, and never blocks or resumes anything; the runners keep stopping new entries
by themselves on stale data, errors and failed reconciliation, exactly as before. (Sultan approved that
the monitor may later block entries on a critical finding; that is a separate, reviewed change.)

It never claims protection: stops can be gapped through, can fill worse than their price, and depend
on Binance being up.
"""
from __future__ import annotations

import json
import os
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from bot.analysis.indicators import percentile, realized_vol
from bot.analysis.resample import daily
from bot.core.config import AppConfig
from bot.core.money import pct
from bot.dashboard import JOURNAL, SOURCES, Account, _d, _journal, read_account
from bot.exchange.interface import INTERVAL_MS, Candle
from bot.risk.models import RejectCode

INFO, WARNING, CRITICAL = "info", "warning", "critical"
ORDER = {CRITICAL: 0, WARNING: 1, INFO: 2}
SILENT_S = 2400                   # no journal line for 40 minutes (runners write a status every 15)
STALE_MARGIN_S = 600              # a finished candle may be this late before prices count as stale
CANDLE_INTERVAL = "4h"            # the interval H2b and the runners decide on
VOL_DAYS, VOL_YEAR = 30, 365
VOL_HIGH = Decimal("0.9")         # 30-day volatility in the top 10% of its past year
MOVE_TIMES_VOL = 3                # a daily move beyond 3 x the 30-day volatility
REPEAT_MAX, REPEAT_DAYS = 3, 3    # the same coin refused more than 3 times in 3 days
REFUSALS = {"entry_refused", "entry_rejected", "entry_blocked", "entry_skipped"}
FORWARD_MIN_TRADES, FORWARD_MIN_DAYS = 30, 365      # docs/h2b-plan.md, section 3
FINDINGS_FILE = "findings.jsonl"
DRAWDOWN = RejectCode.DRAWDOWN.value
LIMITS = ("Stops are not a guarantee: a price can gap through a stop, a stop can fill worse than its "
          "price, and stops depend on Binance being up.")


@dataclass(frozen=True)
class Finding:
    level: str
    rule: str
    subject: str                  # an account (live, testnet, forward, paper) or a coin
    reason: str
    data: dict[str, str] = field(default_factory=dict)

    def line(self) -> str:
        nums = ", ".join(f"{k} {v}" for k, v in self.data.items())
        return f"{self.level.upper():<8} {self.subject:<8} {self.rule}: {self.reason}" + (f" [{nums}]" if nums else "")


def _s(v: Any) -> str:
    if isinstance(v, Decimal):
        return f"{v:.4f}".rstrip("0").rstrip(".") if v != v.to_integral_value() else str(int(v))
    return str(v)


def _f(level: str, rule: str, subject: str, reason: str, **data: Any) -> Finding:
    return Finding(level, rule, subject, reason, {k: _s(v) for k, v in data.items() if v is not None})


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        d = json.loads(path.read_text())
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- accounts
def account_findings(home: Path, now: float, cfg: AppConfig, groups: dict[str, str]) -> list[Finding]:
    out: list[Finding] = []
    for name, folder, money in SOURCES:
        a = read_account(Path(home), name, folder, money)
        if a.found:
            out += _one_account(Path(home) / folder, a, now, cfg, groups)
    return out


def _one_account(d: Path, a: Account, now: float, cfg: AppConfig, groups: dict[str, str]) -> list[Finding]:
    r = cfg.risk
    out = [_f(WARNING, "unreadable file", a.name, p) for p in a.problems]
    if a.last_line_at is None or now - a.last_line_at > SILENT_S:
        mins = None if a.last_line_at is None else int((now - a.last_line_at) // 60)
        out.append(_f(CRITICAL, "data outage", a.name, "the runner has written nothing recently: is the service down?",
                      minutes_silent=mins, limit_minutes=SILENT_S // 60))
    events = _journal(d / JOURNAL)
    health = [e for e in events if e.get("event") in ("status", "mark_failed", "market_data_down", "market_data_back")]
    if health and health[-1].get("event") in ("mark_failed", "market_data_down"):
        out.append(_f(CRITICAL, "data outage", a.name, "the runner's last attempt to read prices or value the account failed",
                      since=_utc(health[-1].get("at")), detail=str(health[-1].get("reason") or "")[:160]))

    for b in a.breaches:
        if b in ("RECONCILIATION_MISMATCH", "UNPROTECTED_POSITION"):
            last = next((e for e in reversed(events) if e.get("event") in ("reconciliation_mismatch", "unprotected")), {})
            out.append(_f(CRITICAL, "inconsistent balances", a.name,
                          "the exchange and the bot's ledger disagree; new entries are blocked until an operator resumes",
                          breach=b, detail=str(last.get("detail") or "")[:160]))
        elif b == DRAWDOWN:
            out.append(_f(CRITICAL, "drawdown", a.name, "the drawdown breaker has tripped; it clears only after a review",
                          drawdown_pct=a.drawdown_pct, breaker_pct=r.drawdown_breaker_pct))
        else:
            out.append(_f(WARNING, "breaker", a.name, "a breaker is active; new entries are blocked", breach=b))

    if a.drawdown_pct is not None and DRAWDOWN not in a.breaches:
        if a.drawdown_pct >= r.drawdown_breaker_pct:
            out.append(_f(CRITICAL, "drawdown", a.name, "drawdown is at the breaker",
                          drawdown_pct=a.drawdown_pct, breaker_pct=r.drawdown_breaker_pct))
        elif a.drawdown_pct >= r.drawdown_breaker_pct / 2:
            out.append(_f(WARNING, "drawdown", a.name, "drawdown is past half the breaker",
                          drawdown_pct=a.drawdown_pct, breaker_pct=r.drawdown_breaker_pct))

    out += _period_losses(d, a, now, cfg)
    out += _concentration(a, cfg, groups)
    out += _repeats(a, events, now)
    if a.name == "forward":
        out += _forward_evidence(d, a, now)
    return out


def _utc(t: Any) -> str | None:
    try:
        return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(float(t)))
    except (TypeError, ValueError):
        return None


def _period_losses(d: Path, a: Account, now: float, cfg: AppConfig) -> list[Finding]:
    saved = _read_json(d / "account.json") or _read_json(d / "state.json") or {}
    p = saved.get("periods") or {}
    if a.equity is None or not p:
        return []
    today = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
    out = []
    for label, anchor, start, flows, limit in (
            ("daily", "day_anchor", "day_start_equity", "day_flows", cfg.risk.daily_loss_pct),
            ("weekly", "week_anchor", "week_start_equity", "week_flows", cfg.risk.weekly_loss_pct)):
        base, moved = _d(p.get(start)), _d(p.get(flows)) or Decimal(0)
        if not base or base <= 0:
            continue
        if label == "daily" and not str(p.get(anchor) or "").startswith(today):
            continue                                    # the runner has not rolled into today yet: no daily figure
        loss_pct = (base + moved - a.equity) / base * 100
        if loss_pct >= limit / 2:
            out.append(_f(WARNING, f"{label} loss", a.name, f"the {label} loss is past half its limit",
                          loss_pct=loss_pct.quantize(Decimal("0.01")), limit_pct=limit))
    return out


def _concentration(a: Account, cfg: AppConfig, groups: dict[str, str]) -> list[Finding]:
    if not a.open_trades or not a.equity or a.equity <= 0:
        return []
    out = []
    per_group: Counter[str] = Counter()
    for t in a.open_trades:
        sym = str(t.get("symbol"))
        per_group[groups.get(sym, "majors" if sym in ("BTCUSDT", "ETHUSDT") else sym)] += 1
        qty, entry = _d(t.get("qty")), _d(t.get("entry"))
        if qty and entry and qty * entry > pct(cfg.risk.max_coin_exposure_pct) * a.equity:
            out.append(_f(WARNING, "concentration", a.name, f"{sym} holds more than its share of the account",
                          coin=sym, share_pct=(qty * entry / a.equity * 100).quantize(Decimal("0.1")),
                          limit_pct=cfg.risk.max_coin_exposure_pct))
    for g, n in sorted(per_group.items()):
        if n > cfg.risk.max_per_corr_group:
            out.append(_f(WARNING, "concentration", a.name, f"more open trades in correlation group {g} than allowed",
                          group=g, open=n, limit=cfg.risk.max_per_corr_group))
    return out


def _repeats(a: Account, events: list[dict[str, Any]], now: float) -> list[Finding]:
    since = now - REPEAT_DAYS * 86400
    n = Counter(str(e.get("symbol")) for e in events
                if e.get("event") in REFUSALS and e.get("symbol") and float(e.get("at") or 0) >= since)
    return [_f(WARNING, "repeated signals", a.name, f"{sym} was proposed and not traded {k} times in {REPEAT_DAYS} days",
               coin=sym, times=k, limit=REPEAT_MAX) for sym, k in sorted(n.items()) if k > REPEAT_MAX]


def _forward_evidence(d: Path, a: Account, now: float) -> list[Finding]:
    ft = _read_json(d / "forward.json") or {}
    try:
        days = int((now - float(ft["started_at"])) // 86400)
    except (KeyError, TypeError, ValueError):
        return []
    if a.closed >= FORWARD_MIN_TRADES and days >= FORWARD_MIN_DAYS:
        return []
    return [_f(INFO, "insufficient evidence", a.name, "the H2b forward test is too young to judge H2b",
               closed_trades=a.closed, needed_trades=FORWARD_MIN_TRADES, days=days, needed_days=FORWARD_MIN_DAYS)]


# ---------------------------------------------------------------- markets
def market_findings(symbol: str, candles: list[Candle], bid: Decimal | None, ask: Decimal | None,
                    now: float, cfg: AppConfig, interval: str = CANDLE_INTERVAL) -> list[Finding]:
    """Stale prices, abnormal spread and abnormal volatility for one coin, from finished candles and its book."""
    out: list[Finding] = []
    if not candles:
        return [_f(CRITICAL, "stale prices", symbol, "no finished candles")]
    late_s = (now * 1000 - (candles[-1].close_time + 1)) / 1000
    if late_s > INTERVAL_MS[interval] / 1000 + STALE_MARGIN_S:
        out.append(_f(CRITICAL, "stale prices", symbol, "the newest finished candle is too old",
                      last_close=_utc((candles[-1].close_time + 1) / 1000), late_minutes=int(late_s // 60)))
    if bid is not None and ask is not None and bid > 0 and ask >= bid:
        spread = (ask - bid) / ((ask + bid) / 2) * 100
        if spread > cfg.risk.max_spread_pct:
            out.append(_f(WARNING, "abnormal spread", symbol, "the spread is above the risk engine's limit (R-11)",
                          spread_pct=spread.quantize(Decimal("0.0001")), limit_pct=cfg.risk.max_spread_pct))
    days = daily(candles)
    closes = [c.close for c in days]
    vols = realized_vol(closes, VOL_DAYS) if len(closes) > VOL_DAYS else []
    today = vols[-1] if vols else None
    earlier = [v for v in vols[-(VOL_YEAR + 1):-1] if v is not None]
    if today is None or len(earlier) < VOL_YEAR:
        out.append(_f(INFO, "insufficient evidence", symbol, "not enough daily history to judge volatility",
                      days=len(days), needed=VOL_DAYS + VOL_YEAR + 1))
        return out
    level = percentile(earlier, VOL_HIGH)
    if today >= level:
        out.append(_f(WARNING, "abnormal volatility", symbol, "30-day volatility is in the top 10% of its past year",
                      vol_pct=(today * 100).quantize(Decimal("0.01")), top10_from_pct=(level * 100).quantize(Decimal("0.01"))))
    move = abs((closes[-1] / closes[-2]).ln())
    if today > 0 and move > MOVE_TIMES_VOL * today:
        out.append(_f(WARNING, "abnormal volatility", symbol, "the last daily move was beyond 3 x the 30-day volatility",
                      move_pct=(move * 100).quantize(Decimal("0.01")), vol_pct=(today * 100).quantize(Decimal("0.01")),
                      day=_utc(days[-1].open_time / 1000)))
    return out


def gather_market(data: Any, symbols: list[str], now: float, cfg: AppConfig) -> list[Finding]:
    """Reads public prices through the read-only market data service; any failure is a finding, never a crash."""
    out: list[Finding] = []
    try:
        books = data.books(list(symbols))
    except Exception as e:
        books = {}
        out.append(_f(CRITICAL, "data outage", "binance", "could not read prices", detail=f"{type(e).__name__}: {e}"[:160]))
    try:
        skew = int(data.venue.clock_skew_ms())
        if abs(skew) > cfg.risk.max_clock_skew_ms:
            out.append(_f(CRITICAL, "clock", "server", "the server clock differs from Binance's beyond the limit (R-13)",
                          skew_ms=skew, limit_ms=cfg.risk.max_clock_skew_ms))
    except Exception as e:
        out.append(_f(CRITICAL, "data outage", "binance", "could not read Binance's time",
                      detail=f"{type(e).__name__}: {e}"[:160]))
    need = (VOL_DAYS + VOL_YEAR + 1 + 3) * (86_400_000 // INTERVAL_MS[CANDLE_INTERVAL])
    for s in symbols:
        try:
            candles = data.closed_candles(s, CANDLE_INTERVAL, limit=need)
        except Exception as e:
            out.append(_f(CRITICAL, "data outage", s, "could not read candles", detail=f"{type(e).__name__}: {e}"[:160]))
            continue
        b = books.get(s)
        out += market_findings(s, candles, getattr(b, "bid", None), getattr(b, "ask", None), now, cfg)
    return out


# ---------------------------------------------------------------- output
def check(home: Path, now: float, cfg: AppConfig, groups: dict[str, str], data: Any = None,
          symbols: list[str] | None = None) -> list[Finding]:
    found = account_findings(home, now, cfg, groups)
    if data is not None and symbols:
        found += gather_market(data, symbols, now, cfg)
    return sorted(found, key=lambda f: (ORDER[f.level], f.subject, f.rule))


def render(findings: list[Finding], now: float, market_checked: bool = True) -> str:
    n = Counter(f.level for f in findings)
    head = (f"Risk monitor, {_utc(now)} (read-only; changes nothing). "
            f"{n[CRITICAL]} critical, {n[WARNING]} warnings, {n[INFO]} notes.")
    lines = [head]
    if not market_checked:
        lines.append("Prices were not checked (--no-market): stale prices, spread, clock and volatility are unknown.")
    if not [f for f in findings if f.level != INFO]:
        lines.append("No warnings.")
    lines += [f.line() for f in findings]
    lines.append(LIMITS)
    return "\n".join(lines) + "\n"


def record(findings: list[Finding], folder: Path, now: float) -> Path:
    """Appends every finding, and one line for the run itself, to <folder>/findings.jsonl."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / FINDINGS_FILE
    n = Counter(f.level for f in findings)
    rows = [{"at": now, **asdict(f)} for f in findings]
    rows.append({"at": now, "run": {CRITICAL: n[CRITICAL], WARNING: n[WARNING], INFO: n[INFO]}})
    with open(path, "a") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in rows))
        fh.flush()
        os.fsync(fh.fileno())
    return path
