"""T-SEC-001 and module boundaries, checked by reading the source."""
import ast
import inspect
from pathlib import Path

from binance_sdk_spot.rest_api import SpotRestAPI
from binance_sdk_spot.rest_api.api import AccountApi, GeneralApi, MarketApi, TradeApi

from bot.exchange.binance import READ_ONLY_CALLS

SRC = Path(__file__).resolve().parents[2] / "src" / "bot"
NETWORK = {"requests", "httpx", "aiohttp", "urllib3", "websockets", "socket", "binance_sdk_spot", "binance_common"}
SDK_ADAPTER = SRC / "exchange" / "binance.py"


def imports_of(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module if node.module.startswith("bot") else node.module.split(".")[0])
    return out


def test_T_SEC_001_no_wallet_or_withdrawal_code_anywhere():
    for f in SRC.rglob("*.py"):
        text = f.read_text()
        assert "binance_sdk_wallet" not in text, f
        assert "/capital/withdraw" not in text and "/asset/transfer" not in text, f


def test_risk_and_portfolio_do_no_io():
    for pkg in ("risk", "portfolio", "core"):
        for f in (SRC / pkg).rglob("*.py"):
            assert not (imports_of(f) & NETWORK), f"{f} imports a network library"


def test_risk_never_imports_news_or_llm():
    for f in (SRC / "risk").rglob("*.py"):
        assert not any(m.startswith(("bot.news", "bot.llm")) for m in imports_of(f)), f


def test_only_the_adapter_imports_the_binance_sdk():
    for f in SRC.rglob("*.py"):
        if f != SDK_ADAPTER:
            assert not (imports_of(f) & {"binance_sdk_spot", "binance_common"}), f"{f} imports the Binance SDK"


def test_read_only_allowlist_is_only_get_endpoints():
    sdk_api = (AccountApi, GeneralApi, MarketApi, TradeApi)
    for name in READ_ONLY_CALLS:
        impl = next(getattr(c, name) for c in sdk_api if hasattr(c, name))
        assert 'method="GET"' in inspect.getsource(impl), f"{name} is not a GET endpoint"


def test_no_source_file_names_an_order_endpoint():
    """Module 2 is read-only: no order-changing or generic-request SDK method may appear anywhere."""
    sdk_api = (AccountApi, GeneralApi, MarketApi, TradeApi)
    writes = {n for c in sdk_api for n, fn in inspect.getmembers(c, inspect.isfunction)
              if not n.startswith("_") and 'method="GET"' not in inspect.getsource(fn)}
    forbidden = (writes | {"send_request", "send_signed_request"}) - READ_ONLY_CALLS
    assert {n for n, _ in inspect.getmembers(SpotRestAPI, inspect.isfunction)} >= forbidden
    assert {"new_order", "delete_order", "order_list_opoco", "send_signed_request"} <= forbidden
    for f in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(f.read_text())):
            name = None
            if isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                name = node.value
            assert name not in forbidden, f"{f}:{getattr(node, 'lineno', '?')} references SDK method {name!r}"
