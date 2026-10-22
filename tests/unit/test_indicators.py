"""Module 4: indicators. Known values, and no look-ahead: changing later candles never moves earlier values."""
from decimal import Decimal as Dec

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from bot.analysis import indicators as ind
from bot.exchange.interface import Candle


def C(o, h, l, c, t=0):
    return Candle(t, Dec(o), Dec(h), Dec(l), Dec(c), Dec(1), t + 1, Dec(1), True)


def test_sma_highest_lowest_pct_change():
    v = [Dec(x) for x in (1, 2, 3, 4, 5)]
    assert ind.sma(v, 3) == [None, None, Dec(2), Dec(3), Dec(4)]
    assert ind.highest(v, 2) == [None, Dec(2), Dec(3), Dec(4), Dec(5)]
    assert ind.lowest(v, 2) == [None, Dec(1), Dec(2), Dec(3), Dec(4)]
    assert ind.pct_change(v, 2) == [None, None, Dec(2), Dec(1), Dec(5) / 3 - 1]


def test_true_range_and_atr():
    cs = [C(10, 12, 9, 11), C(11, 15, 10, 14), C(14, 14, 8, 9), C(9, 10, 9, 10)]
    assert ind.true_range(cs) == [Dec(3), Dec(5), Dec(6), Dec(1)]
    a = ind.atr(cs, 2)
    assert a[0] is None and a[1] == Dec(4)                    # mean of the first two
    assert a[2] == (Dec(4) * 1 + 6) / 2 and a[3] == (a[2] * 1 + 1) / 2


def test_rsi_extremes_and_zscore():
    up = [Dec(i) for i in range(1, 20)]
    assert ind.rsi(up, 14)[-1] == 100
    down = list(reversed(up))
    assert ind.rsi(down, 14)[-1] == 0
    assert ind.zscore([Dec(1)] * 5, 3)[-1] == 0
    assert ind.zscore([Dec(1), Dec(2), Dec(3)], 3)[-1] == 1


def test_realized_vol():
    flat = [Dec(100)] * 10
    assert ind.realized_vol(flat, 5)[-1] == 0
    assert ind.realized_vol(flat, 5)[4] is None and ind.realized_vol(flat, 5)[5] == 0


def test_bad_windows_refused():
    with pytest.raises(ValueError):
        ind.sma([Dec(1)], 0)
    with pytest.raises(ValueError):
        ind.zscore([Dec(1)], 1)


# ---------------------------------------------------------------- no look-ahead
prices = st.lists(st.decimals(min_value=Dec("1"), max_value=Dec("100000"), places=2), min_size=30, max_size=60)


def candles_from(ps):
    return [C(p, p * Dec("1.01"), p * Dec("0.99"), p, i) for i, p in enumerate(ps)]


SERIES_FNS = {
    "sma": lambda v: ind.sma(v, 5), "highest": lambda v: ind.highest(v, 5), "lowest": lambda v: ind.lowest(v, 5),
    "pct_change": lambda v: ind.pct_change(v, 5), "realized_vol": lambda v: ind.realized_vol(v, 5),
    "rsi": lambda v: ind.rsi(v, 5), "zscore": lambda v: ind.zscore(v, 5),
}


def leaks(fn, values, cut, replacement) -> bool:
    """True if values after `cut` change any output at or before `cut`."""
    a = fn(values)
    b = fn(values[:cut + 1] + replacement)
    return a[:cut + 1] != b[:cut + 1]


@settings(max_examples=300, deadline=None)
@given(ps=prices, data=st.data())
def test_no_indicator_looks_ahead(ps, data):
    cut = data.draw(st.integers(0, len(ps) - 2))
    future = data.draw(st.lists(st.decimals(min_value=Dec("1"), max_value=Dec("100000"), places=2),
                                min_size=len(ps) - cut - 1, max_size=len(ps) - cut - 1))
    for name, fn in SERIES_FNS.items():
        assert not leaks(fn, ps, cut, future), name
    assert not leaks(ind.atr, candles_from(ps), cut, candles_from(future)), "atr"


def test_the_look_ahead_check_catches_a_cheat():
    """A deliberately cheating indicator (uses the next value) must be caught."""
    def cheat(v):
        return [v[i + 1] if i + 1 < len(v) else None for i in range(len(v))]
    ps = [Dec(x) for x in range(1, 40)]
    assert leaks(cheat, ps, 10, [Dec(999)] * (len(ps) - 11))
