"""T-SEC-001 and module boundaries, checked by reading the source."""
import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "bot"
NETWORK = {"requests", "httpx", "aiohttp", "urllib3", "websockets", "socket", "binance_sdk_spot"}


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


TRADING_SDK_METHODS = {"new_order", "order_oco", "order_list_oco", "order_list_oto", "order_list_otoco",
                       "order_list_opo", "order_list_opoco", "order_cancel_replace", "order_test",
                       "order_amend_keep_priority", "sor_order", "sor_order_test", "delete_order",
                       "delete_order_list", "delete_open_orders"}


def test_module2_adapter_is_read_only():
    """The Binance adapter only sends GET requests to an allow-list with no order endpoints."""
    from bot.exchange.binance import READ_ENDPOINTS

    path = SRC / "exchange" / "binance.py"
    tree = ast.parse(path.read_text())
    called_attrs = {n.func.attr for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert not (called_attrs & TRADING_SDK_METHODS)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "_get":
            endpoint = node.args[0]
            assert isinstance(endpoint, ast.Constant), "endpoints must be literal strings"
            assert endpoint.value in READ_ENDPOINTS, endpoint.value
        if isinstance(node, ast.Constant) and node.value in {"POST", "PUT", "DELETE"}:
            raise AssertionError(f"write HTTP method {node.value} appears in the read-only adapter")
    assert all("order" not in e.lower() and "withdraw" not in e and "transfer" not in e.lower() for e in READ_ENDPOINTS)


def test_risk_never_imports_news_or_llm():
    for f in (SRC / "risk").rglob("*.py"):
        assert not any(m.startswith(("bot.news", "bot.llm")) for m in imports_of(f)), f


def test_module3_paper_and_market_data_never_touch_the_network_or_orders():
    """The simulator and market data service only use the SpotReadVenue interface."""
    for pkg in ("paper", "market"):
        for f in (SRC / pkg).rglob("*.py"):
            imports = imports_of(f)
            assert not (imports & NETWORK), f"{f} imports a network library"
            assert "bot.exchange.binance" not in imports, f"{f} must use bot.exchange.interface, not the adapter"
            assert not any(m.startswith(("bot.news", "bot.llm")) for m in imports), f
            text = f.read_text()
            assert "/api/" not in text and "/sapi/" not in text, f
            called = {n.func.attr for n in ast.walk(ast.parse(text))
                      if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
            assert not (called & TRADING_SDK_METHODS), f


def test_paper_fill_model_is_pure():
    assert imports_of(SRC / "paper" / "fills.py") <= {"__future__", "decimal", "bot.core.money"}


def _functions(path: Path) -> dict[str, ast.FunctionDef]:
    return {n.name: n for n in ast.walk(ast.parse(path.read_text())) if isinstance(n, ast.FunctionDef)}


def _calls(fn: ast.FunctionDef) -> set[str]:
    out = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Call):
            out.add(n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", ""))
    return out


def test_every_simulated_entry_goes_through_the_risk_engine():
    """Rule 1: only submit() opens a position, and submit() calls evaluate() first."""
    fns = _functions(SRC / "paper" / "sim.py")
    openers = {name for name, fn in fns.items() if "_open_position" in _calls(fn)}
    assert openers == {"submit"}
    assert "evaluate" in _calls(fns["submit"])
    # positions are only created in _open_position
    for name, fn in fns.items():
        for n in ast.walk(fn):
            if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "SimPosition":
                assert name == "_open_position", name
    # no public method takes a Decision as input
    for name, fn in fns.items():
        if not name.startswith("_"):
            for a in fn.args.args:
                ann = ast.unparse(a.annotation) if a.annotation else ""
                assert "Decision" not in ann, f"{name} accepts a Decision"
