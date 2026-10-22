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


def test_risk_never_imports_news_or_llm():
    for f in (SRC / "risk").rglob("*.py"):
        assert not any(m.startswith(("bot.news", "bot.llm")) for m in imports_of(f)), f
