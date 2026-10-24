"""The daily report (docs/intel-plan.md, section 6 and step 3; approved by Sultan on 24 Oct 2026).

Once a day, after the 00:00 UTC daily close (04:00 Muscat), one report from what the bot already
measures: the market observations (`bot.intel.market`), the risk monitor's findings (`bot.monitor`),
H2b's own daily checks (`bot.signal_report`), the accounts (as the dashboard reads them) and the
research record. It is saved to `~/reports/<date>.md` and `.json`; a short version can go to Telegram.

The conclusion follows fixed rules and is never forced positive:
- **Pause analysis** when any finding is critical (missing or stale data, a silent runner, a failed
  reconciliation, a tripped drawdown breaker);
- **Sufficient evidence for further simulation (H2b)** only while H2b, the one strategy that passed
  walk-forward, has its forward test running without a breach;
- **Insufficient evidence** otherwise (the default).

Read-only: it builds text from the inputs it is given. It imports nothing that trades, never writes
into a runner's folder, and never blocks or resumes anything. Scenarios are H2b's own rules applied
to today's prices, not forecasts; news is not covered yet (a later step) and the report says so.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from bot.dashboard import Account
from bot.intel.market import Observation, breadth
from bot.monitor import CRITICAL, INFO, LIMITS, WARNING, Finding
from bot.risk.models import RejectCode
from bot.signal_report import CoinSignal

PAUSE = "Pause analysis"
SUFFICIENT = "Sufficient evidence for further simulation (H2b)"
INSUFFICIENT = "Insufficient evidence"
DRAWDOWN = RejectCode.DRAWDOWN.value
SENT_FILE = "sent.json"
HEADER = ("Research and monitoring of a small capped account and simulated accounts. "
          "Not a forecast, not advice, no profit target.")

# The research record, copied from real server runs (docs/h2b-plan.md, docs/h4-plan.md, CLAUDE.md).
# Walk-forward test windows: 2022-01 to 2025-09; the holdout year (2025-10 to 2026-10) was used once, by H2.
RESEARCH = (
    ("H1 4h breakout", "walk-forward, BTC and ETH", "did not qualify"),
    ("H2 time-series momentum", "walk-forward 121 trades; holdout 27 trades, -2.16 USDT per trade",
     "rejected (failed the holdout)"),
    ("H3 4h mean reversion", "walk-forward, BTC and ETH", "did not qualify"),
    ("H2b blended momentum", "walk-forward 104 trades, +1.90 USDT per trade with doubled costs",
     "passed walk-forward; forward test on new prices running; trades live with a capped amount"),
    ("H4 hourly pullbacks", "walk-forward 331 trades, -1.51 USDT per trade (-5.68 with doubled costs)",
     "did not qualify"),
    ("I1-I3 intraday (15m)", "not run yet", "planned (docs/intraday-plan.md)"),
)
FORWARD_MIN_TRADES, FORWARD_MIN_DAYS = 30, 365


@dataclass
class Report:
    date: str
    at: float
    conclusion: str
    reason: str
    sections: list[tuple[str, list[str]]] = field(default_factory=list)

    def text(self) -> str:
        out = [f"Daily report, {self.date} ({time.strftime('%H:%M UTC', time.gmtime(self.at))}). {HEADER}",
               f"Conclusion: {self.conclusion}. {self.reason}"]
        for title, lines in self.sections:
            out += ["", title] + [f"  {x}" for x in lines]
        out += ["", LIMITS]
        return "\n".join(out) + "\n"

    def short(self) -> list[str]:
        """Lines for Telegram: the conclusion, the market in one line, risk and every warning."""
        keep = {"Market regime and uncertainty", "Risk"}
        out = [f"📋 Daily report {self.date}", f"Conclusion: {self.conclusion}. {self.reason}"]
        for title, lines in self.sections:
            if title in keep:
                out += [title + ":"] + [f"• {x}" for x in lines]
        return out + [f"Full report: ~/reports/{self.date}.md (and the dashboard)."]

    def to_json(self) -> dict[str, Any]:
        return {**asdict(self), "sections": [{"title": t, "lines": ls} for t, ls in self.sections]}


# ---------------------------------------------------------------- building
def _find(accounts: list[Account], name: str) -> Account | None:
    return next((a for a in accounts if a.name == name and a.found), None)


def _forward_days(forward: dict[str, Any], now: float) -> int | None:
    try:
        return int((now - float(forward["started_at"])) // 86400)
    except (KeyError, TypeError, ValueError):
        return None


def conclude(findings: list[Finding], accounts: list[Account], forward: dict[str, Any]) -> tuple[str, str]:
    crit = [f for f in findings if f.level == CRITICAL]
    if crit:
        what = "; ".join(f"{f.rule} ({f.subject})" for f in crit[:3]) + (" ..." if len(crit) > 3 else "")
        return PAUSE, f"Critical: {what}. Nothing here is reliable until this is fixed."
    fwd = _find(accounts, "forward")
    if fwd is not None and forward and DRAWDOWN not in fwd.breaches:
        return SUFFICIENT, ("H2b passed walk-forward and its forward test is running without a breach; "
                            "it is still far too early to judge it (see Research evidence).")
    if fwd is None or not forward:
        return INSUFFICIENT, "No forward test is running, so no strategy has evidence on new prices."
    return INSUFFICIENT, "The H2b forward test has hit its drawdown breaker."


def _p(x: Decimal | None) -> str:
    return "?" if x is None else f"{x:+.2f}%"


def _market(obs: list[Observation], findings: list[Finding]) -> tuple[list[str], list[str]]:
    if not obs:
        return ["Prices were not read: no market view today."], []
    b = breadth(obs)
    regime = [f"Breadth: {b.above_avg50} of {b.known} coins above their 50-day average."]
    detail = []
    for o in obs:
        if o.error:
            regime.append(f"{o.symbol}: could not read prices ({o.error}).")
            continue
        side = "?" if o.above_avg50 is None else ("above" if o.above_avg50 else "below")
        rank = "" if o.vol_rank_pct is None else f", higher than {o.vol_rank_pct}% of its year"
        regime.append(f"{o.symbol}: regime {o.regime}; {side} its 50-day average ({_p(o.from_avg50_pct)}); "
                      f"volatility {o.vol30_pct}% a day{rank}.")
        vol = "" if o.volume_vs_avg is None else f"; volume {o.volume_vs_avg}x its 30-day average"
        detail.append(f"{o.symbol} {o.day}: close {o.close}; day {_p(o.change_1d_pct)}, week {_p(o.change_7d_pct)}{vol}.")
    unusual = [f for f in findings if f.rule == "abnormal volatility"]
    regime.append("Uncertainty: " + ("; ".join(f"{f.subject} {f.reason}" for f in unusual) if unusual else
                                     "no coin shows abnormal volatility today (calm markets can still move "
                                     "sharply)."))
    return regime, detail


def _scenarios(signals: list[CoinSignal] | None, accounts: list[Account]) -> list[str]:
    out = []
    if signals is None:
        out.append("H2b's checks could not be worked out today (no forward test settings or no prices).")
    for s in signals or []:
        if s.error:
            out.append(f"{s.symbol}: could not read prices ({s.error}).")
        elif s.enter:
            out.append(f"{s.symbol}: H2b's buy conditions are met; the risk engine still decides, and nothing "
                       f"is bought if the coin is already held.")
        elif s.lookbacks:
            strong = sum(lb.strong for lb in s.lookbacks)
            bars = ", ".join(f"{lb.days}d {lb.ret * 100:+.1f}% (needs above {lb.needed * 100:+.1f}%)" for lb in s.lookbacks)
            pause = "" if s.paused is None else (" Volatility pause ON." if s.paused else "")
            out.append(f"{s.symbol}: no buy: {strong} of 3 returns strong, H2b needs 2. {bars}.{pause}")
        else:
            out.append(f"{s.symbol}: no buy: {s.why}.")
    for a in accounts:
        for t in a.open_trades:
            out.append(f"{a.name} holds {t.get('symbol')}: stop {t.get('stop')}, target {t.get('target')}; "
                       f"H2b also sells once 2 of its 3 returns are below zero.")
    out.append("These are H2b's own rules applied to today's prices, not predictions.")
    return out


def _risk(accounts: list[Account], findings: list[Finding]) -> list[str]:
    out = []
    for a in accounts:
        if not a.found:
            continue
        start = "" if a.started_with is None else f" of {a.started_with:,.2f} at the start"
        eq = "?" if a.equity is None else f"{a.equity:,.2f}"
        dd = "?" if a.drawdown_pct is None else f"{a.drawdown_pct}%"
        out.append(f"{a.name} ({a.money}): {eq} USDT{start}; drawdown {dd} (breaker 6%); {a.mode}; "
                   f"{len(a.open_trades)} open, {a.closed} closed.")
    bad = [f for f in findings if f.level in (CRITICAL, WARNING)]
    out += [f"{f.level.upper()}: {f.subject} {f.rule}: {f.reason}." for f in bad] or ["No risk warnings."]
    return out


def _evidence(accounts: list[Account], forward: dict[str, Any], now: float) -> list[str]:
    out = [f"{name}: {tested}; {verdict}." for name, tested, verdict in RESEARCH]
    fwd = _find(accounts, "forward")
    days = _forward_days(forward, now)
    if fwd is not None and days is not None:
        out.append(f"H2b forward test: day {days} of at least {FORWARD_MIN_DAYS}; {fwd.closed} closed trades of "
                   f"at least {FORWARD_MIN_TRADES}. Simulated results so far are not evidence yet.")
    else:
        out.append("H2b forward test: not found on this server.")
    return out


def _data(findings: list[Finding], obs: list[Observation], market_checked: bool) -> list[str]:
    data_rules = {"data outage", "stale prices", "clock", "unreadable file"}
    bad = [f for f in findings if f.rule in data_rules]
    out = [f"{f.level.upper()}: {f.subject} {f.rule}: {f.reason}." for f in bad]
    if not market_checked:
        out.append("Prices were not checked in this run.")
    elif not bad:
        days = sorted({o.day for o in obs if o.day})
        out.append("Prices fresh, the server clock matches Binance, every runner is reporting"
                   + (f"; last finished day {days[-1]}." if days else "."))
    short = [f for f in findings if f.level == INFO]
    out += [f"Note: {f.subject}: {f.reason}." for f in short]
    return out


def build(now: float, accounts: list[Account], findings: list[Finding], obs: list[Observation],
          signals: list[CoinSignal] | None, forward: dict[str, Any], market_checked: bool = True) -> Report:
    conclusion, reason = conclude(findings, accounts, forward)
    regime, detail = _market(obs, findings)
    sections = [
        ("Market regime and uncertainty", regime),
        ("News", ["Not covered yet: official sources (central banks, US data releases, regulators, Binance "
                  "status) come in a later step. No news is shown rather than unverified news."]),
        ("Major-asset observations", detail or ["None today."]),
        ("Scenarios and invalidation (H2b's rules)", _scenarios(signals, accounts)),
        ("Risk", _risk(accounts, findings)),
        ("Research evidence", _evidence(accounts, forward, now)),
        ("Data quality and API health", _data(findings, obs, market_checked)),
    ]
    return Report(time.strftime("%Y-%m-%d", time.gmtime(now)), now, conclusion, reason, sections)


# ---------------------------------------------------------------- saving and sending
def _atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def save(report: Report, folder: Path) -> Path:
    """<date>.md, <date>.json and latest.md, each written whole or not at all. A second run the same day
    replaces that day's files."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    md = folder / f"{report.date}.md"
    _atomic(md, report.text())
    _atomic(folder / f"{report.date}.json", json.dumps(report.to_json(), indent=1, default=str))
    _atomic(folder / "latest.md", report.text())
    return md


def _sent(folder: Path) -> dict[str, str]:
    try:
        d = json.loads((Path(folder) / SENT_FILE).read_text())
        return {str(k): str(v) for k, v in d.items()} if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def send_once(report: Report, folder: Path, telegram: Any, resend: bool = False) -> str:
    """Send the short version at most once per day. The day is marked 'sending' before the message goes
    and 'sent' after, so a run that stopped half-way is never repeated without --resend."""
    from bot.alerts import messages

    folder = Path(folder)
    state = _sent(folder)
    if not resend and state.get(report.date) == "sent":
        return "already sent today"
    if not resend and state.get(report.date) == "sending":
        return "a previous run stopped while sending; it may or may not have arrived (use --resend)"
    state[report.date] = "sending"
    _atomic(folder / SENT_FILE, json.dumps(state, indent=1))
    try:
        for m in messages(report.short()):
            telegram.send(m)
    except Exception:
        state[report.date] = "failed"                      # refused or unreachable: the next run tries again
        _atomic(folder / SENT_FILE, json.dumps(state, indent=1))
        raise
    state[report.date] = "sent"
    _atomic(folder / SENT_FILE, json.dumps(state, indent=1))
    return "sent"
