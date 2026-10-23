"""The H2b forward test (docs/h2b-plan.md step C, approved by Sultan): H2b on live prices with
simulated money, in its own account. It does not count toward the Stage 6 paper review.

`start` fixes everything once, in `<state dir>/forward.json`, before the first trade:
- the settings, chosen by the walk-forward's own rule on the last 24 research months
  (2023-10 to 2025-09; research data only, the spent holdout year is not used);
- the coins and correlation groups from the plan (BTC and ETH together, XRP and BNB alone);
- fingerprints of H2b's code and of the config. If either changes later, the runner
  refuses to start: any change to H2b restarts the test from zero, in a new folder.

Length: at least 12 months and at least 30 closed trades, whichever ends later, at most
24 months. If the runner misses more than 10% of the time, both dates move by the time
missed. Early end only for failure: the 6% drawdown breaker tripping fails H2b at once.
Pass or fail is judged once, at the end, by the end-of-test report (6 checks).
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from bot.backtest.periods import HOLDOUT_START_MS
from bot.backtest.replay import CostModel
from bot.backtest.walkforward import TRAIN_MONTHS, Window, _add, _ms, choose
from bot.core.config import AppConfig
from bot.core.money import D
from bot.exchange.filters import SymbolRules
from bot.exchange.interface import INTERVAL_MS, Candle
from bot.risk.models import RejectCode
from bot.strategies.h2b_blend import BlendedMomentum

FORWARD_FILE = "forward.json"
CONFIRM_PHRASE = "I am starting the H2b forward test"
STRATEGY = "H2b"
PLAN_GROUPS = {"BTCUSDT": "majors", "ETHUSDT": "majors", "XRPUSDT": "XRPUSDT", "BNBUSDT": "BNBUSDT"}
MIN_DAYS, MAX_DAYS, MIN_TRADES = 365, 730, 30
OUTAGE_SHARE = Decimal("0.10")
DAY_S = 86_400
_SOURCES = ("strategies/h2b_blend.py", "strategies/base.py")      # H2b's own code: a change restarts the test


class ForwardError(PermissionError):
    pass


@dataclass(frozen=True)
class ForwardTest:
    strategy: str
    params: dict[str, str]
    symbols: list[str]
    corr_groups: dict[str, str]
    started_at: float
    operator: str
    train_score: str | None
    config_sha256: str
    strategy_sha256: str
    git_commit: str


def strategy_sha256() -> str:
    root = Path(__file__).resolve().parent
    h = hashlib.sha256()
    for rel in _SOURCES:
        h.update(rel.encode() + b"\0" + (root / rel).read_bytes())
    return h.hexdigest()


def config_sha256(cfg: AppConfig) -> str:
    return hashlib.sha256(cfg.model_dump_json().encode()).hexdigest()


def settings_window() -> Window:
    ty, tm = _add(2025, 10, -TRAIN_MONTHS)
    return Window(_ms(ty, tm), HOLDOUT_START_MS, HOLDOUT_START_MS, HOLDOUT_START_MS)


def check_coins(symbols: list[str], groups: dict[str, str]) -> None:
    got = {s: groups.get(s, "majors") for s in symbols}
    if got != PLAN_GROUPS:
        raise ForwardError(f"the coins and groups {got} are not the plan's {PLAN_GROUPS}; the forward test "
                           f"uses exactly the plan's coins (docs/h2b-plan.md section 2)")


# ---------------------------------------------------------------- start (once)
def start(state_dir: Path, research: dict[str, list[Candle]], rules: dict[str, SymbolRules], cfg: AppConfig,
          base_cfg: AppConfig, operator: str, confirm: str, now: float, groups: dict[str, str], git_commit: str,
          progress=None) -> ForwardTest:
    """`cfg` has the four coins as its universe; `base_cfg` is the config file as loaded (fingerprinted)."""
    state_dir = Path(state_dir)
    if confirm != CONFIRM_PHRASE:
        raise ForwardError(f'type --confirm "{CONFIRM_PHRASE}" exactly to start the forward test')
    if not operator.strip():
        raise ForwardError("the forward test needs an operator name")
    if (state_dir / FORWARD_FILE).exists() or (state_dir / "state.json").exists():
        raise ForwardError(f"{state_dir} already holds a forward test or a paper account; a forward test never "
                           f"restarts in place (a new one needs a new, empty folder)")
    check_coins(list(research), groups)
    symbols = sorted(research, key=list(PLAN_GROUPS).index)
    w = settings_window()
    say = progress or (lambda m: None)
    say("H2b: choosing settings on 2023-10 to 2025-09 (research data only); 9 runs")
    chosen, score = choose(BlendedMomentum(symbols), research, rules, cfg, CostModel(), w, groups)
    ft = ForwardTest(strategy=STRATEGY, params={k: str(v) for k, v in chosen.params().items()}, symbols=symbols,
                     corr_groups={s: groups.get(s, "majors") for s in symbols}, started_at=now, operator=operator,
                     train_score=None if score is None else str(score), config_sha256=config_sha256(base_cfg),
                     strategy_sha256=strategy_sha256(), git_commit=git_commit)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = state_dir / f".{FORWARD_FILE}.tmp"
    tmp.write_text(json.dumps(asdict(ft), indent=1))
    try:
        os.link(tmp, state_dir / FORWARD_FILE)          # fails if one appeared meanwhile: never overwritten
    except FileExistsError:
        raise ForwardError(f"a forward test appeared in {state_dir} while this one was being set up; "
                           f"it was kept and this start was dropped") from None
    finally:
        tmp.unlink(missing_ok=True)
    return ft


def load(state_dir: Path) -> ForwardTest:
    path = Path(state_dir) / FORWARD_FILE
    if not path.exists():
        raise ForwardError(f"no forward test in {state_dir}; Sultan starts one with: bot forward start")
    try:
        return ForwardTest(**json.loads(path.read_text()))
    except Exception as e:
        raise ForwardError(f"{path} is unreadable ({type(e).__name__}: {e}); it is never replaced") from None


def check_unchanged(ft: ForwardTest, base_cfg: AppConfig) -> None:
    """Refuse to run when H2b's code or the config changed since the start (plan: a change restarts the test)."""
    if strategy_sha256() != ft.strategy_sha256:
        raise ForwardError("H2b's code (h2b_blend.py or strategies/base.py) changed since the forward test started. "
                           "The plan says any change to H2b restarts the test from zero: restore the code, or start "
                           "a new forward test in a new folder")
    if config_sha256(base_cfg) != ft.config_sha256:
        raise ForwardError("the config changed since the forward test started; the risk limits are part of the "
                           "test: restore config/default.yaml, or start a new forward test in a new folder")


def make_strategy(ft: ForwardTest) -> BlendedMomentum:
    s = BlendedMomentum(list(ft.symbols))
    if set(ft.params) != set(s.params()):
        raise ForwardError(f"saved settings {sorted(ft.params)} do not match H2b's {sorted(s.params())}")
    return s.with_params(**{k: D(v) for k, v in ft.params.items()})


# ---------------------------------------------------------------- progress (status; pass/fail is the report's)
@dataclass(frozen=True)
class Progress:
    days: Decimal
    trades: int
    missed_hours: int
    outage_share: Decimal
    earliest_end: float
    latest_end: float
    failed_at: float | None          # the drawdown breaker tripped: H2b fails at once
    state: str                       # running / ready to judge / ended / failed


def progress(ft: ForwardTest, journal: list[dict[str, Any]], last_decided_ms: int | None, trades: int,
             now: float) -> Progress:
    step = INTERVAL_MS[BlendedMomentum.interval]
    logged = sum(int(e.get("candles", 0)) for e in journal if e.get("event") == "decisions_missed")
    newest = int(now * 1000) // step * step
    ongoing = 0 if last_decided_ms is None else max(0, (newest - last_decided_ms) // step - 1)
    missed_hours = (logged + ongoing) * step // 3_600_000
    elapsed = max(now - ft.started_at, 1.0)
    share = D(missed_hours * 3600) / D(elapsed)
    extra = missed_hours * 3600 if share > OUTAGE_SHARE else 0
    earliest, latest = ft.started_at + MIN_DAYS * DAY_S + extra, ft.started_at + MAX_DAYS * DAY_S + extra
    failed = next((float(e["at"]) for e in journal
                   if e.get("event") == "breaker" and e.get("breach") == RejectCode.DRAWDOWN.value), None)
    if failed is not None:
        state = "failed"
    elif now >= latest:
        state = "ended"
    elif now >= earliest and trades >= MIN_TRADES:
        state = "ready to judge"
    else:
        state = "running"
    return Progress(D(now - ft.started_at) / DAY_S, trades, missed_hours, share, earliest, latest, failed, state)


def read_journal(state_dir: Path) -> list[dict[str, Any]]:
    path = Path(state_dir) / "journal.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []


def when(t: float) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(t))
