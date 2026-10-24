"""Baseline: random entries with the same exits and sizing (design package).

At each finished candle, each coin has a fixed chance of a buy idea. With
`exits` set to a strategy, stops, holding horizon, trailing and exit signals
are that strategy's, so the comparison isolates the strategy's entries: a
strategy that cannot beat this has no edge. Without `exits`, a plain ATR exit
is used. Runs with fixed seeds, so results repeat exactly.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from decimal import Decimal

from bot.exchange.interface import INTERVAL_MS, Candle
from bot.risk.models import TradeIntent
from bot.strategies.base import Action, AtrExit, CandleSource, OpenTrade, Strategy, entry_limit, make_intent


CHANCE_PER_4H = 1 / 30                         # about one idea per coin every 5 days


@dataclass
class RandomEntry:
    symbols: list[str]
    seed: int
    chance_per_candle: float = CHANCE_PER_4H   # about one idea per coin every 5 days on 4h candles
    exit: AtrExit = field(default_factory=AtrExit)
    exits: Strategy | None = None              # borrow this strategy's exits
    interval: str = "4h"
    name: str = "RANDOM"

    def __post_init__(self) -> None:
        if not 0 < self.chance_per_candle <= 1:
            raise ValueError("chance_per_candle must be in (0, 1]")
        self._rng = random.Random(self.seed)

    @classmethod
    def with_exits_of(cls, strategy: Strategy, symbols: list[str], seed: int) -> "RandomEntry":
        """Random entries on the strategy's own candles with its exits, at the same number of ideas
        per day whatever the interval (on 4h candles exactly the default chance)."""
        chance = CHANCE_PER_4H * INTERVAL_MS[strategy.interval] / INTERVAL_MS["4h"]
        return cls(symbols, seed=seed, chance_per_candle=chance, exits=strategy, interval=strategy.interval)

    def params(self) -> dict[str, object]:
        p = {"seed": self.seed, "chance_per_candle": self.chance_per_candle}
        if self.exits is None:
            p.update(atr_mult=str(self.exit.atr_mult), horizon_hours=self.exit.horizon_hours)
        else:
            p.update(exits_from=self.exits.name, **{f"exit_{k}": v for k, v in self.exits.params().items()})
        return p

    def with_params(self, **p: object) -> "RandomEntry":
        raise TypeError("the random baseline has no tunable settings")

    def history_needed(self) -> int:
        return self.exits.history_needed() if self.exits else self.exit.atr_n * 3

    def exit_plan(self, candles: list[Candle], entry: Decimal) -> tuple[Decimal, int] | None:
        if self.exits is not None:
            return self.exits.exit_plan(candles, entry)
        stop = self.exit.stop_for(candles, entry)
        return None if stop is None else (stop, self.exit.horizon_hours)

    def propose(self, data: CandleSource, now: float) -> list[TradeIntent]:
        out = []
        for s in self.symbols:
            if self._rng.random() >= self.chance_per_candle:     # drawn for every coin every candle
                continue
            candles = data.closed_candles(s, self.interval, limit=self.history_needed())
            if not candles:
                continue
            entry = entry_limit(candles[-1].close)
            plan = self.exit_plan(candles, entry)
            if plan:
                intent = make_intent(s, entry, plan[0], self.name, plan[1], "random baseline: no signal to invalidate")
                if intent is not None:
                    out.append(intent)
        return out

    def manage(self, data: CandleSource, now: float, open_trades: list[OpenTrade]) -> list[Action]:
        return self.exits.manage(data, now, open_trades) if self.exits is not None else []
