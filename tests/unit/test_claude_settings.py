"""Claude Code on the server (Remote Control): it may read the bot's status freely, but never its secrets,
never the live switch, and never the steps only Sultan takes (CLAUDE.md rules 2, 5 and 6)."""
import json
from pathlib import Path

from bot.core.config import LIVE_CONFIRM_PHRASE

ROOT = Path(__file__).resolve().parents[2]
SETTINGS = json.loads((ROOT / ".claude" / "settings.json").read_text())
SKILL = (ROOT / ".claude" / "skills" / "trading-status" / "SKILL.md").read_text()


def test_only_read_only_commands_run_without_asking():
    allow = SETTINGS["permissions"]["allow"]
    for rule in allow:
        assert rule.startswith("Bash("), rule
        words = rule[5:-1].split()
        assert words[-1] in ("status", "log", "*", "signals", "-m"), rule
        assert not {"resume", "block", "start", "baseline", "trade", "setup", "check"} & set(words), rule


def test_secrets_and_the_live_switch_are_denied():
    deny = set(SETTINGS["permissions"]["deny"])
    assert {"Read(./.env)", "Edit(./.env)", "Write(./.env)"} <= deny
    assert {"Edit(./config/live.yaml)", "Write(./config/live.yaml)", "Edit(./config/default.yaml)"} <= deny
    for step in ("live start *", "live baseline *", "forward start *", "testnet start *", "paper trade *"):
        assert f"Bash({step})" in deny and f"Bash(scripts/{step})" in deny


def test_the_skill_never_carries_the_switch():
    assert LIVE_CONFIRM_PHRASE not in SKILL and "live_trading: true" not in SKILL.lower()
    assert SKILL.startswith("---\nname: trading-status\n")
    for never in ("Never read, print or edit `.env`", "Never resume on your own", "There is no command to buy or sell"):
        assert never in SKILL
