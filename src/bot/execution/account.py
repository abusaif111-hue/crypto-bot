"""The bot's trading account on an exchange (module 5d): a capped capital inside a bigger account.

The testnet account holds far more than the bot trades with (about 10,000 USDT and coins Binance
gives every account), and a live account may hold Sultan's own coins. So the bot trades a fixed
capital, set by an operator at the start, and its equity is that capital plus everything that
has changed since: for USDT and each watched coin, (total now - total at the start), coins
valued at the best bid. That includes every fee, whatever closed a trade (the bot, a stop or a
take-profit) and any dust. Nothing else may move these balances: reconciliation checks the coins
against the ledger. Free USDT for the risk engine is the smaller of the bot's cash and the
account's free USDT.

On the real exchange (`cap_equity`) the risk engine never sizes on more than the capital: profits and
any USDT someone deposits do not make trades bigger; losses do make them smaller.

P&L periods, the drawdown index and the operating modes work as in the paper account (same
classes), and are saved after every change. Fail closed: a restart restores the account with
entries blocked and every breaker kept; an unreadable file is an error and is never reset.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from bot.core.config import AppConfig
from bot.core.money import ZERO, D
from bot.execution.ledger import OPEN, Ledger
from bot.portfolio.accounting import EquityIndex, MissingPrice, PeriodTracker
from bot.risk.models import AccountState, Mode, OpenPosition
from bot.risk.modes import ModeController, Transition

ACCOUNT_FILE = "account.json"
VERSION = 1
QUOTE = "USDT"


class AccountError(RuntimeError):
    pass


def as_utc(t: float) -> datetime:
    return datetime.fromtimestamp(t, timezone.utc)


@dataclass
class TradingAccount:
    path: Path
    cfg: AppConfig
    capital: Decimal                       # USDT the bot trades with, set at the start
    start: dict[str, Decimal]              # account totals (USDT and watched coins) at the start
    started_at: float
    started_by: str
    fee_rate: Decimal = Decimal("0.001")
    cap_equity: bool = False               # live: the risk engine sees min(equity, capital)
    index: EquityIndex = field(default_factory=EquityIndex)
    periods: PeriodTracker = field(default_factory=PeriodTracker)
    modes: ModeController = field(init=False)
    last_equity: Decimal | None = field(default=None, init=False)   # the true equity at the last mark

    def __post_init__(self) -> None:
        self.modes = ModeController(self.cfg)            # starts ENTRIES_BLOCKED

    # ------------------------------------------------------------ start, save, load
    @staticmethod
    def begin(directory: Path, cfg: AppConfig, ledger: Ledger, totals: dict[str, Decimal], free_usdt: Decimal,
              capital: Decimal, operator: str, now: float, fee_rate: Decimal = Decimal("0.001"),
              cap_equity: bool = False, max_capital: Decimal | None = None) -> "TradingAccount":
        """Start the account. Only with a baseline, nothing open, and the capital available in free USDT."""
        path = Path(directory) / ACCOUNT_FILE
        capital = D(capital)
        if path.exists():
            raise AccountError(f"{path} already exists; the account is started once")
        if not operator.strip():
            raise AccountError("starting the account needs an operator name")
        if not ledger.baseline:
            raise AccountError("the ledger has no baseline yet (bot reconcile --set-baseline)")
        if ledger.active():
            raise AccountError("a trade is PENDING or OPEN in the ledger; reconcile it first")
        if capital <= 0 or capital > free_usdt:
            raise AccountError(f"capital must be positive and at most the free USDT ({free_usdt})")
        if max_capital is not None and capital > max_capital:
            raise AccountError(f"capital {capital} is above the ceiling of {max_capital} USDT")
        start = {a: D(totals.get(a, ZERO)) for a in sorted({QUOTE, *ledger.baseline})}
        acct = TradingAccount(path, cfg, capital, start, now, operator.strip(), D(fee_rate), cap_equity)
        acct.index.start(capital)
        acct.periods.update(as_utc(now), capital)
        acct.save()
        return acct

    def save(self) -> None:
        body = {"version": VERSION, "capital": str(self.capital), "start": {a: str(q) for a, q in self.start.items()},
                "started_at": self.started_at, "started_by": self.started_by, "fee_rate": str(self.fee_rate),
                "cap_equity": self.cap_equity,
                "index": {"units": str(self.index.units), "value_per_unit": str(self.index.value_per_unit),
                          "peak_value_per_unit": str(self.index.peak_value_per_unit)},
                "periods": {"day_anchor": _iso(self.periods.day_anchor), "week_anchor": _iso(self.periods.week_anchor),
                            "day_start_equity": str(self.periods.day_start_equity),
                            "week_start_equity": str(self.periods.week_start_equity),
                            "day_flows": str(self.periods.day_flows), "week_flows": str(self.periods.week_flows)},
                "modes": {"mode": self.modes.mode.value, "active_breaches": sorted(self.modes.active_breaches),
                          "history": [{**asdict(t), "at": t.at.isoformat(), "from_mode": t.from_mode.value,
                                       "to_mode": t.to_mode.value} for t in self.modes.history]}}
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w") as f:
            json.dump(body, f, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    @staticmethod
    def load(directory: Path, cfg: AppConfig, now: float) -> "TradingAccount":
        """Restore the account with new entries blocked; breakers stay as they were."""
        path = Path(directory) / ACCOUNT_FILE
        if not path.exists():
            raise AccountError(f"no account in {path.parent} yet (bot testnet start)")
        try:
            d = json.loads(path.read_text())
            if d.get("version") != VERSION:
                raise AccountError(f"account file version {d.get('version')!r}, expected {VERSION}")
            acct = TradingAccount(path, cfg, D(d["capital"]), {a: D(q) for a, q in d["start"].items()},
                                  float(d["started_at"]), d["started_by"], D(d["fee_rate"]), bool(d.get("cap_equity")))
            ix, pr, md = d["index"], d["periods"], d["modes"]
            acct.index.units, acct.index.value_per_unit, acct.index.peak_value_per_unit = (
                D(ix["units"]), D(ix["value_per_unit"]), D(ix["peak_value_per_unit"]))
            acct.periods.day_anchor = _from_iso(pr["day_anchor"])
            acct.periods.week_anchor = _from_iso(pr["week_anchor"])
            for k in ("day_start_equity", "week_start_equity", "day_flows", "week_flows"):
                setattr(acct.periods, k, D(pr[k]))
            acct.modes.active_breaches = set(md["active_breaches"])
            acct.modes.history = [Transition(datetime.fromisoformat(t["at"]), Mode(t["from_mode"]), Mode(t["to_mode"]),
                                             t["actor"], t["reason"]) for t in md["history"]]
            acct.modes.mode = Mode(md["mode"])
        except AccountError as e:
            raise AccountError(f"{path}: {e}; fix it by hand, it is never reset") from None
        except Exception as e:
            raise AccountError(f"{path} is unreadable ({type(e).__name__}: {e}); fix it by hand, it is never reset") \
                from None
        if acct.capital <= 0 or acct.index.units <= 0:
            raise AccountError(f"{path} is inconsistent (capital or drawdown index); fix it by hand")
        acct.modes.block_entries("system", "restarted: entries stay blocked until an operator resumes", as_utc(now))
        return acct

    # ------------------------------------------------------------ valuation
    def equity(self, totals: dict[str, Decimal], bids: dict[str, Decimal]) -> Decimal:
        """Capital plus the change in USDT and every watched coin since the start, coins at the best bid."""
        out = self.capital
        for asset, at_start in self.start.items():
            change = D(totals.get(asset, ZERO)) - at_start
            if change == 0:
                continue
            if asset == QUOTE:
                out += change
                continue
            bid = bids.get(asset + QUOTE)
            if bid is None or bid <= 0:
                raise MissingPrice(f"no bid for {asset}{QUOTE}; the bot's equity cannot be valued")
            out += change * bid
        return out

    def cash(self, totals: dict[str, Decimal]) -> Decimal:
        """The bot's USDT: capital plus the change in the account's USDT since the start."""
        return self.capital + D(totals.get(QUOTE, ZERO)) - self.start[QUOTE]

    def state(self, ledger: Ledger, totals: dict[str, Decimal], free_usdt: Decimal, bids: dict[str, Decimal],
              corr_groups: dict[str, str], now: float, entries_ok: bool = True) -> AccountState:
        """Mark equity (P&L periods, drawdown, breakers) and return what the risk engine sees.
        `entries_ok` False (this round's reconciliation was not clean) blocks entries for this round only."""
        equity = self.equity(totals, bids)
        self.last_equity = equity
        events = self.periods.update(as_utc(now), equity)
        self.index.mark(equity)
        state = self._state(ledger, equity, totals, free_usdt, bids, corr_groups, entries_ok)
        if "day" in events:
            self.modes.on_day_rollover(state, as_utc(now))
        if "week" in events:
            self.modes.on_week_rollover(state)
        self.modes.on_state(state, as_utc(now))
        return self._state(ledger, equity, totals, free_usdt, bids, corr_groups, entries_ok)

    def _state(self, ledger: Ledger, equity: Decimal, totals: dict[str, Decimal], free_usdt: Decimal,
               bids: dict[str, Decimal], corr_groups: dict[str, str], entries_ok: bool) -> AccountState:
        mode = self.modes.mode
        if mode is Mode.RUNNING and not (self.modes.entries_allowed and entries_ok):
            mode = Mode.ENTRIES_BLOCKED
        positions = []
        for t in ledger.trades.values():
            if t.status != OPEN:
                continue
            bid = bids.get(t.symbol)
            if bid is None or bid <= 0:
                raise MissingPrice(f"no bid for {t.symbol}; the open trade cannot be valued")
            positions.append(OpenPosition(symbol=t.symbol, qty=t.received_qty, avg_entry=t.entry_price,
                                          stop_price=t.stop_price, current_bid=bid,
                                          corr_group=corr_groups.get(t.symbol, "majors")))
        sized_on = min(equity, self.capital) if self.cap_equity else equity
        return AccountState(mode=mode, equity=sized_on, free_quote=max(ZERO, min(self.cash(totals), D(free_usdt))),
                            day_start_equity=self.periods.day_start_equity,
                            week_start_equity=self.periods.week_start_equity,
                            daily_pnl=self.periods.daily_pnl(equity), weekly_pnl=self.periods.weekly_pnl(equity),
                            drawdown_pct=self.index.drawdown_pct, positions=tuple(positions),
                            taker_fee_rate=self.fee_rate)

    def resume(self, operator: str, reason: str, state: AccountState, now: float) -> None:
        """Same rules as everywhere: a name, a written reason, and no active breach or fault."""
        self.modes.resume(operator, reason, state, as_utc(now))


def _iso(t: datetime | None) -> str | None:
    return t.isoformat() if t else None


def _from_iso(s: Any) -> datetime | None:
    return datetime.fromisoformat(s) if s else None
