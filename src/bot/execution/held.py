"""Orders the bot holds itself (H5, docs/h5-plan.md section 4, Sultan's decision 4 of 31 Oct 2026).

Binance Spot does not accept a protected stop-type buy as the first order of an OTOCO or OPOCO list (the
working order may only be LIMIT or LIMIT_MAKER: `binance-sdk-spot`'s OrderListOtocoWorkingTypeEnum and
OrderListOpocoWorkingTypeEnum). So the runner keeps each of H5's daily orders here instead of on the
exchange, and checks the price every round. Once the best ask trades above the trigger and is still at or
below the limit, the order becomes an ordinary intent (no trigger, limit = the order's limit) and goes the
usual way: written PENDING, then `entry.submit()`, which runs `evaluate()` on the account as it is then and
sends one protected order list (BUY LIMIT FOK with its stop-loss and take-profit). An ask above the limit
is not chased: the order keeps waiting in case the price comes back. One attempt per order; it expires at
`valid_until`; every held order is dropped as soon as entries are blocked. Nothing here sends an order.

The file `held-orders.json` in the runner's folder keeps them across a restart; an unreadable file is an
error and is never reset.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path

from bot.core.money import D
from bot.risk.models import TradeIntent

HELD_FILE = "held-orders.json"


class HeldError(RuntimeError):
    pass


@dataclass(frozen=True)
class HeldOrder:
    symbol: str
    strategy: str
    trigger: Decimal
    limit: Decimal
    stop: Decimal
    target: Decimal
    horizon_hours: int
    invalidation: str
    placed_at: float
    valid_until: float

    @classmethod
    def from_intent(cls, intent: TradeIntent, now: float) -> "HeldOrder":
        if intent.trigger_price is None or intent.valid_until is None:
            raise HeldError("only an order placed in advance (a trigger and an expiry) is held")
        return cls(intent.symbol, intent.strategy, D(intent.trigger_price), D(intent.entry_price),
                   D(intent.stop_price), D(intent.take_profit_price), intent.horizon_hours, intent.invalidation,
                   now, float(intent.valid_until))

    def reached(self, ask: Decimal) -> bool:
        """The price has traded above the trigger and is still within the limit: buy now."""
        return self.trigger < D(ask) <= self.limit

    def intent(self) -> TradeIntent:
        """The ordinary intent sent once the trigger is reached: a limit at the order's limit, no trigger."""
        return TradeIntent(symbol=self.symbol, side="BUY", entry_price=self.limit, stop_price=self.stop,
                           take_profit_price=self.target, strategy=self.strategy, horizon_hours=self.horizon_hours,
                           invalidation=self.invalidation)

    def to_json(self) -> dict:
        return {k: str(v) if isinstance(v, Decimal) else v for k, v in asdict(self).items()}

    @classmethod
    def from_json(cls, d: dict) -> "HeldOrder":
        return cls(d["symbol"], d["strategy"], D(d["trigger"]), D(d["limit"]), D(d["stop"]), D(d["target"]),
                   int(d["horizon_hours"]), d["invalidation"], float(d["placed_at"]), float(d["valid_until"]))


def load(directory: Path) -> dict[str, HeldOrder]:
    path = Path(directory) / HELD_FILE
    if not path.exists():
        return {}
    try:
        rows = json.loads(path.read_text())
        return {r["symbol"]: HeldOrder.from_json(r) for r in rows}
    except (ValueError, KeyError, TypeError) as e:
        raise HeldError(f"{path} cannot be read ({e}); it is never reset: fix or move it by hand") from None


def save(directory: Path, orders: dict[str, HeldOrder]) -> None:
    path = Path(directory) / HELD_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps([o.to_json() for o in orders.values()], indent=1))
    os.replace(tmp, path)


# ---------------------------------------------------------------- the H5 switch for one runner account
H5_FILE = "h5.json"


def enable_h5(directory: Path, operator: str, capital: Decimal, account_capital: Decimal, now: float) -> dict:
    """Sultan's step: let this runner (testnet or live) trade H5, sized on at most `capital` of its account.
    Written once; read when the runner starts (a restart is needed). Never more than the account's capital."""
    path = Path(directory) / H5_FILE
    if path.exists():
        raise HeldError(f"{path} already exists: H5 was switched on before; move it aside to change it")
    if not operator.strip():
        raise HeldError("switching H5 on needs an operator name")
    capital = D(capital)
    if not 0 < capital <= D(account_capital):
        raise HeldError(f"H5's capital must be above 0 and at most the account's capital ({account_capital} USDT)")
    rec = {"enabled_by": operator.strip(), "enabled_at": now, "capital": str(capital)}
    path.write_text(json.dumps(rec, indent=1))
    return rec


def load_h5(directory: Path) -> Decimal | None:
    """H5's capital for this runner, or None while H5 is not switched on. An unreadable file is an error."""
    path = Path(directory) / H5_FILE
    if not path.exists():
        return None
    try:
        return D(json.loads(path.read_text())["capital"])
    except (ValueError, KeyError, TypeError, ArithmeticError) as e:
        raise HeldError(f"{path} cannot be read ({e}); fix or move it by hand") from None
