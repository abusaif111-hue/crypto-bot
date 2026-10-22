"""Baseline: random entries with the same exits and sizing (design package).

At each finished candle, each coin has a fixed chance of a buy idea. Exits and
sizing are the same as a real strategy's, so a strategy that cannot beat this
has no edge in its entries. Runs with many fixed seeds, so results repeat exactly.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field

from bot.risk.models import TradeIntent
from bot.strategies.base import AtrExit, CandleSource, entry_limit, make_intent


@dataclass
class RandomEntry:
    symbols: list[str]
    seed: int
    chance_per_candle: float = 1 / 30          # about one idea per coin every 5 days on 4h candles
    exit: AtrExit = field(default_factory=AtrExit)
    interval: str = "4h"
    name: str = "RANDOM"

    def __post_init__(self) -> None:
        if not 0 < self.chance_per_candle <= 1:
            raise ValueError("chance_per_candle must be in (0, 1]")
        self._rng = random.Random(self.seed)

    def params(self) -> dict[str, object]:
        return {"seed": self.seed, "chance_per_candle": self.chance_per_candle,
                "atr_mult": str(self.exit.atr_mult), "horizon_hours": self.exit.horizon_hours}

    def propose(self, data: CandleSource, now: float) -> list[TradeIntent]:
        out = []
        for s in self.symbols:
            if self._rng.random() >= self.chance_per_candle:     # drawn for every coin every candle
                continue
            candles = data.closed_candles(s, self.interval, limit=self.exit.atr_n * 3)
            if not candles:
                continue
            entry = entry_limit(candles[-1].close)
            intent = make_intent(s, entry, self.exit.stop_for(candles, entry), self.name,
                                 self.exit.horizon_hours, "random baseline: no signal to invalidate")
            if intent is not None:
                out.append(intent)
        return out
