"""Made-up candle histories for module 4 tests. Deterministic; not market data."""
from __future__ import annotations

import random
from decimal import Decimal as Dec

from bot.exchange.filters import parse_symbol
from bot.exchange.interface import Candle
from tests.conftest import exchange_info_symbol

H4 = 14_400_000


def walk(start_ms: int, n: int, price: str = "30000", seed: int = 1, drift: float = 0.0,
         vol: float = 0.01, step_ms: int = H4) -> list[Candle]:
    rng = random.Random(seed)
    p = Dec(price)
    out = []
    for i in range(n):
        o = p
        c = (o * Dec(str(1 + drift + rng.gauss(0, vol)))).quantize(Dec("0.01"))
        c = max(c, Dec("1.00"))
        hi = (max(o, c) * Dec(str(1 + abs(rng.gauss(0, vol / 2))))).quantize(Dec("0.01"))
        lo = (min(o, c) * Dec(str(1 - abs(rng.gauss(0, vol / 2))))).quantize(Dec("0.01"))
        t = start_ms + i * step_ms
        out.append(Candle(open_time=t, open=o, high=hi, low=max(lo, Dec("0.50")), close=c, volume=Dec("1000"),
                          close_time=t + step_ms - 1, quote_volume=Dec("50000000"), closed=True))
        p = c
    return out


def rules(symbols=("BTCUSDT", "ETHUSDT")):
    return {s: parse_symbol(exchange_info_symbol(s)) for s in symbols}
