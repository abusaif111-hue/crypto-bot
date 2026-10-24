"""Which coins H4 is tested on: decision 4 of docs/h4-plan.md (Sultan chose (a), 24 Oct 2026).

H4 uses 1h candles, four times as many as 4h. If the four screened coins (BTC, ETH, XRP, BNB)
do not fit the server's memory, H4 is tested and forward-tested on BTC and ETH only. The
measurement alone decides, once, before any H4 result exists:

- the peak memory of one H4 backtest over the whole research period is measured in a fresh
  process, first with BTC alone, then with BTC and ETH; the H4 results of these runs are
  thrown away unseen (only the memory figure leaves the process);
- four coins are estimated as two coins plus twice the extra memory the second coin took;
- four coins fit when that estimate plus a 30 MB margin is within the memory available
  (Linux MemAvailable, with the bot's services running as usual) when the check starts.

The result is written once to `h4-coins.json` in the data folder and never overwritten, so the
check cannot be repeated until it gives a wanted answer. `bot backtest run --strategy H4` uses
only the coins recorded there.
"""
from __future__ import annotations

import json
import multiprocessing
import time
from pathlib import Path
from typing import Any, Callable

from bot.backtest import history
from bot.backtest.universe import ALWAYS, available_memory_mb

DECISION_FILE = "h4-coins.json"
MARGIN_MB = 30
INTERVAL = "1h"
RULE = ("four coins when (2-coin peak + 2 x (2-coin peak - 1-coin peak)) + 30 MB <= MemAvailable at the "
        "start of the check; otherwise BTC and ETH only (docs/h4-plan.md, decision 4a)")


class H4CoinsError(RuntimeError):
    pass


def _peak_child(conn, data_dir: str, symbols: list[str], cfg_dump: dict, groups: dict | None) -> None:
    """Runs in a fresh process: one full research-period H4 backtest; sends back only the peak memory."""
    import resource

    from bot.backtest.engine import run
    from bot.backtest.periods import RESEARCH_START_MS
    from bot.backtest.replay import CostModel
    from bot.core.config import AppConfig
    from bot.strategies.h4_pullback import TrendPullback

    try:
        candles = {s: history.load(Path(data_dir), s, INTERVAL) for s in symbols}
        rules = {s: history.load_rules(Path(data_dir), s) for s in symbols}
        cfg = AppConfig.model_validate(cfg_dump)
        run(TrendPullback(symbols), candles, rules, cfg, CostModel(), RESEARCH_START_MS, edge=True,
            corr_groups=groups)
        conn.send(("ok", resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024))   # KB -> MB on Linux
    except Exception as e:
        conn.send(("error", f"{type(e).__name__}: {e}"))
    finally:
        conn.close()


def measure_peak_mb(data_dir: Path, symbols: list[str], cfg, groups: dict | None) -> int:
    ctx = multiprocessing.get_context("spawn")
    parent, child = ctx.Pipe(duplex=False)
    p = ctx.Process(target=_peak_child, args=(child, str(data_dir), symbols, cfg.model_dump(mode="json"), groups))
    p.start()
    child.close()
    try:
        status, value = parent.recv()
    except EOFError:
        status, value = "error", "the measuring process stopped without an answer (out of memory?)"
    p.join()
    if status != "ok":
        raise H4CoinsError(f"memory measurement with {', '.join(symbols)} failed: {value}")
    return int(value)


def decide(data_dir: Path, four: list[str], groups: dict[str, str], cfg, operator: str,
           measure: Callable[..., int] = measure_peak_mb, available: Callable[[], int | None] = available_memory_mb,
           now: Callable[[], float] = time.time) -> dict[str, Any]:
    data_dir = Path(data_dir)
    path = data_dir / DECISION_FILE
    if path.exists():
        raise H4CoinsError(f"H4's coins were already decided ({path}); the check is never repeated")
    if not operator.strip():
        raise H4CoinsError("the check needs an operator name")
    if len(four) != 4 or tuple(four[:2]) != ALWAYS:
        raise H4CoinsError(f"the screen should give BTCUSDT, ETHUSDT and two more coins, not {four}")
    missing = [s for s in four if not history._csv_path(data_dir, s, INTERVAL).exists()]
    if missing:
        raise H4CoinsError(f"no 1h history for {', '.join(missing)}; run: bot backtest fetch --interval 1h --screened")
    free = available()
    if free is None:
        raise H4CoinsError("cannot read the available memory (/proc/meminfo), so nothing can be decided")
    one = measure(data_dir, four[:1], cfg, groups)
    two = measure(data_dir, four[:2], cfg, groups)
    estimate = two + 2 * max(0, two - one)
    fits = estimate + MARGIN_MB <= free
    coins = list(four) if fits else list(ALWAYS)
    out = {"strategy": "H4", "interval": INTERVAL, "coins": coins,
           "groups": groups if fits else None,             # as the screen gave them; None: the config's own
           "screened": list(four), "four_coins_fit": fits,
           "available_mb": free, "peak_1_coin_mb": one, "peak_2_coins_mb": two, "estimate_4_coins_mb": estimate,
           "margin_mb": MARGIN_MB, "rule": RULE, "operator": operator.strip(),
           "decided_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now()))}
    data_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, indent=1))
    tmp.replace(path)
    return out


def load(data_dir: Path) -> dict[str, Any]:
    path = Path(data_dir) / DECISION_FILE
    if not path.exists():
        raise H4CoinsError("H4's coins are not decided yet; run: bot backtest h4-memory --operator <name>")
    try:
        rec = json.loads(path.read_text())
        assert rec["strategy"] == "H4" and rec["coins"] and rec["coins"][:2] == list(ALWAYS)
        return rec
    except Exception as e:
        raise H4CoinsError(f"{path} is damaged ({type(e).__name__}); it is never rewritten, ask for help") from None


def needed_mb(rec: dict[str, Any]) -> int:
    """The measured peak for the recorded coins (the 4-coin figure is the estimate)."""
    return rec["estimate_4_coins_mb"] if rec["four_coins_fit"] else rec["peak_2_coins_mb"]


def render(rec: dict[str, Any]) -> str:
    verdict = ("all four coins fit: H4 is tested on " if rec["four_coins_fit"] else
               "four coins do not fit: H4 is tested on ") + ", ".join(rec["coins"])
    return (f"H4 memory check ({rec['decided_at']}, {rec['operator']}):\n"
            f"  memory available at the start: {rec['available_mb']} MB\n"
            f"  measured peak, BTC alone: {rec['peak_1_coin_mb']} MB; BTC and ETH: {rec['peak_2_coins_mb']} MB\n"
            f"  estimate for four coins: {rec['estimate_4_coins_mb']} MB (+ {rec['margin_mb']} MB margin)\n"
            f"  decision: {verdict}\n"
            f"  rule: {rec['rule']}\n")
