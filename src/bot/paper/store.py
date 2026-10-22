"""Saving and restoring the paper account (module 3).

The simulated account is written to one JSON file after every runner tick, so a
restart or server reboot keeps balances, open positions with their exit
orders, trades, P&L periods, the drawdown index and every breaker.

Fail closed:
- A file that cannot be read or does not match this format stops the runner.
  It is never replaced by a fresh 10,000 USDT account.
- After a restart entries are blocked until an operator resumes, like the real
  bot. Breakers that were active stay active, so a restart never clears one.

Writes go to a temporary file first and then replace the old one, so a crash
mid-write leaves the previous state intact.
"""
from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from bot.core.config import AppConfig
from bot.core.money import D
from bot.exchange.interface import SpotReadVenue
from bot.paper.sim import ClosedTrade, Fill, SimExchange, SimPosition, as_utc
from bot.risk.models import Mode
from bot.risk.modes import Transition

FORMAT_VERSION = 1


class StateError(RuntimeError):
    pass


# ------------------------------------------------------------ to JSON
def plain(v: Any) -> Any:
    if isinstance(v, Decimal):
        return str(v)
    if isinstance(v, uuid.UUID):
        return v.hex
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, Mode):
        return v.value
    if isinstance(v, dict):
        return {k: plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [plain(x) for x in v]
    return v


def to_dict(sim: SimExchange, now: float) -> dict[str, Any]:
    return plain({
        "version": FORMAT_VERSION,
        "saved_at": now,
        "created_at": sim.created_at,
        "starting_quote": sim.starting_quote,
        "fee_rate": sim.fee_rate,
        "quote": sim.quote,
        "positions": [asdict(p) for p in sim.positions.values()],
        "fills": [asdict(f) for f in sim.fills],
        "trades": [asdict(t) for t in sim.trades],
        "bids": dict(sim._bids),
        "index": {"units": sim.index.units, "value_per_unit": sim.index.value_per_unit,
                  "peak_value_per_unit": sim.index.peak_value_per_unit},
        "periods": {"day_anchor": sim.periods.day_anchor, "week_anchor": sim.periods.week_anchor,
                    "day_start_equity": sim.periods.day_start_equity,
                    "week_start_equity": sim.periods.week_start_equity,
                    "day_flows": sim.periods.day_flows, "week_flows": sim.periods.week_flows},
        "modes": {"mode": sim.modes.mode, "active_breaches": sorted(sim.modes.active_breaches),
                  "history": [asdict(t) for t in sim.modes.history]},
    })


def save(sim: SimExchange, path: Path, now: float) -> None:
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w") as f:
        json.dump(to_dict(sim, now), f, indent=1)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ------------------------------------------------------------ from JSON
_DECIMAL_FIELDS = {
    "qty", "entry_price", "entry_fee", "stop_price", "take_profit_price", "planned_risk",
    "price", "fee", "exit_price", "exit_fee", "pnl", "slippage",
}


def _record(cls: type, raw: dict[str, Any]) -> Any:
    kw = {k: D(v) if k in _DECIMAL_FIELDS else v for k, v in raw.items()}
    if "intent_id" in kw:
        kw["intent_id"] = uuid.UUID(hex=kw["intent_id"])
    return cls(**kw)


def from_dict(data: dict[str, Any], cfg: AppConfig, market: SpotReadVenue,
              clock: Callable[[], float]) -> SimExchange:
    if data.get("version") != FORMAT_VERSION:
        raise StateError(f"state file format {data.get('version')!r} is not {FORMAT_VERSION}")
    sim = SimExchange(cfg, market, clock=clock, starting_quote=D(data["starting_quote"]), fee_rate=D(data["fee_rate"]))
    sim.quote = D(data["quote"])
    sim.created_at = float(data["created_at"])
    positions = [_record(SimPosition, p) for p in data["positions"]]
    sim.positions = {p.symbol: p for p in positions}
    sim.fills = [_record(Fill, f) for f in data["fills"]]
    sim.trades = [_record(ClosedTrade, t) for t in data["trades"]]
    sim._bids = {s: D(b) for s, b in data["bids"].items()}

    ix = data["index"]
    sim.index.units, sim.index.value_per_unit, sim.index.peak_value_per_unit = (
        D(ix["units"]), D(ix["value_per_unit"]), D(ix["peak_value_per_unit"]))
    pr = data["periods"]
    sim.periods.day_anchor = datetime.fromisoformat(pr["day_anchor"]) if pr["day_anchor"] else None
    sim.periods.week_anchor = datetime.fromisoformat(pr["week_anchor"]) if pr["week_anchor"] else None
    for k in ("day_start_equity", "week_start_equity", "day_flows", "week_flows"):
        setattr(sim.periods, k, D(pr[k]))

    md = data["modes"]
    sim.modes.active_breaches = set(md["active_breaches"])
    sim.modes.history = [Transition(datetime.fromisoformat(t["at"]), Mode(t["from_mode"]), Mode(t["to_mode"]),
                                    t["actor"], t["reason"]) for t in md["history"]]
    sim.modes.mode = Mode(md["mode"])
    _check(sim)
    return sim


def _check(sim: SimExchange) -> None:
    problems = []
    if sim.quote < 0:
        problems.append("negative USDT balance")
    if sim.index.units <= 0:
        problems.append("drawdown index not started")
    for p in sim.positions.values():
        if p.qty <= 0 or not (p.stop_price < p.take_profit_price):
            problems.append(f"{p.symbol} position is not valid")
        if p.symbol not in sim._bids:
            problems.append(f"{p.symbol} has no saved price")
    if problems:
        raise StateError("saved paper account is inconsistent: " + "; ".join(problems))


def load_or_create(path: Path, cfg: AppConfig, market: SpotReadVenue,
                   clock: Callable[[], float]) -> tuple[SimExchange, bool]:
    """Returns (simulator, restored). A missing file starts a new 10,000 USDT account;
    an unreadable one raises StateError."""
    path = Path(path)
    if not path.exists():
        return SimExchange(cfg, market, clock=clock), False
    try:
        data = json.loads(path.read_text())
        sim = from_dict(data, cfg, market, clock)
    except StateError:
        raise
    except Exception as e:
        raise StateError(f"cannot read the saved paper account {path}: {type(e).__name__}: {e}") from None
    sim.modes.block_entries("system", "restarted: entries stay blocked until an operator resumes", as_utc(clock()))
    return sim, True
