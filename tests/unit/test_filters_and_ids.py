import uuid
from decimal import Decimal as Dec

import pytest

from bot.core.ids import client_order_id, is_valid_client_id
from bot.core.money import floor_to_step
from bot.exchange.filters import limit_order_violations, parse_symbol
from tests.conftest import exchange_info_symbol


def test_T_SIZE_001_floor_never_rounds_up():
    assert floor_to_step(Dec("0.009669"), Dec("0.00001")) == Dec("0.00966")
    assert floor_to_step(Dec("1.99999"), Dec("1")) == Dec("1")
    assert floor_to_step(Dec("-1"), Dec("0.1")) == 0


def test_parse_symbol_reads_filters(btc_rules):
    assert btc_rules.tick_size == Dec("0.01") and btc_rules.step_size == Dec("0.00001")
    assert btc_rules.min_notional == Dec("5") and btc_rules.opo_allowed
    assert btc_rules.market_min_qty is None  # zero values treated as "not set"


def test_T_SIZE_002_valid_order_passes(btc_rules):
    assert limit_order_violations(btc_rules, "BUY", Dec("80000.00"), Dec("0.001"), Dec("80000")) == []


@pytest.mark.parametrize("price,qty,expected", [
    ("80000.005", "0.001", "tickSize"),
    ("80000", "0.000015", "stepSize"),
    ("80000", "0.00005", "notional"),
    ("500000", "0.001", "PERCENT_PRICE_BY_SIDE"),
])
def test_T_SIZE_003_filter_violations(btc_rules, price, qty, expected):
    v = limit_order_violations(btc_rules, "BUY", Dec(price), Dec(qty), Dec("80000"))
    assert any(expected in x for x in v), v


def test_T_SIZE_004_halted_symbol_fails():
    rules = parse_symbol(exchange_info_symbol(status="BREAK"))
    assert any("status" in x for x in limit_order_violations(rules, "BUY", Dec("80000"), Dec("0.001")))


def test_T_SIZE_005_max_asset_filter():
    rules = parse_symbol(exchange_info_symbol(),
                         my_filters={"assetFilters": [{"filterType": "MAX_ASSET", "asset": "BTC", "maxQuantity": "0.5"}]})
    assert any("MAX_ASSET" in x for x in limit_order_violations(rules, "BUY", Dec("80000"), Dec("0.1"), held_base_qty=Dec("0.45")))


def test_T_EXEC_020_client_ids_are_deterministic_and_valid():
    i = uuid.uuid4()
    a, b = client_order_id(i, "entry"), client_order_id(i, "entry")
    assert a == b and is_valid_client_id(a) and len(a) <= 36
    assert client_order_id(i, "protect") != a
    assert client_order_id(uuid.uuid4(), "entry") != a
