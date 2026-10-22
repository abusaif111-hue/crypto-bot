"""Operating modes and circuit breakers (design package section 4).

The controller owns the bot's mode. Any breaker, stale data, failed
reconciliation or an unprotected position moves it out of RUNNING. Getting back
to RUNNING always takes an operator with a written reason, and is refused while
any breach is still active. Every transition is recorded for the audit log.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from bot.core.config import AppConfig
from bot.risk.engine import breached_limits
from bot.risk.models import AccountState, Mode, RejectCode

EMERGENCY_CONFIRM_PHRASE = "FLATTEN ALL POSITIONS"

# Operational faults that block entries until an operator clears them.
FAULTS = {
    "STALE_DATA", "RECONCILIATION_MISMATCH", "UNPROTECTED_POSITION", "CLOCK_SKEW",
    "EXCHANGE_UNAVAILABLE", "RATE_LIMIT_BAN", "AI_UNAVAILABLE", "STABLECOIN_DEPEG",
}


@dataclass(frozen=True)
class Transition:
    at: datetime
    from_mode: Mode
    to_mode: Mode
    actor: str            # "system" or an operator name
    reason: str


@dataclass
class ModeController:
    cfg: AppConfig
    mode: Mode = Mode.ENTRIES_BLOCKED          # fail closed: start blocked until checks pass
    active_breaches: set[str] = field(default_factory=set)
    history: list[Transition] = field(default_factory=list)

    # ---------------------------------------------------------------- internal
    def _set(self, new: Mode, actor: str, reason: str, now: datetime | None = None) -> None:
        if new is self.mode:
            return
        self.history.append(Transition(now or datetime.now(timezone.utc), self.mode, new, actor, reason))
        self.mode = new

    # ---------------------------------------------------------------- system side
    def on_state(self, state: AccountState, now: datetime | None = None) -> list[str]:
        """Call on every equity update. Returns newly detected breaches (for alerting)."""
        found = {b.value for b in breached_limits(state, self.cfg)}
        new = found - self.active_breaches
        self.active_breaches |= found
        if new and self.mode is Mode.RUNNING:
            self._set(Mode.ENTRIES_BLOCKED, "system", "limit reached: " + ", ".join(sorted(new)), now)
        return sorted(new)

    def report_fault(self, fault: str, detail: str = "", now: datetime | None = None) -> None:
        if fault not in FAULTS:
            raise ValueError(f"unknown fault {fault}")
        self.active_breaches.add(fault)
        if self.mode is Mode.RUNNING:
            self._set(Mode.ENTRIES_BLOCKED, "system", f"{fault} {detail}".strip(), now)

    def clear_fault(self, fault: str) -> None:
        """Called by the subsystem that owns the fault once it is fixed. Does not resume trading."""
        self.active_breaches.discard(fault)

    def on_day_rollover(self, state: AccountState, now: datetime | None = None) -> None:
        """New trading day: the daily breach can clear; weekly and drawdown never clear on their own."""
        self.active_breaches.discard(RejectCode.DAILY_LOSS.value)
        self.active_breaches |= {b.value for b in breached_limits(state, self.cfg)}
        if (self.cfg.risk.auto_clear_daily_block and self.mode is Mode.ENTRIES_BLOCKED
                and not self.active_breaches and self._last_block_was_daily_only()):
            self._set(Mode.RUNNING, "system", "new trading day; daily limit reset", now)

    def on_week_rollover(self, state: AccountState) -> None:
        self.active_breaches.discard(RejectCode.WEEKLY_LOSS.value)
        self.active_breaches |= {b.value for b in breached_limits(state, self.cfg)}

    def _last_block_was_daily_only(self) -> bool:
        for t in reversed(self.history):
            if t.to_mode is Mode.ENTRIES_BLOCKED:
                return t.actor == "system" and t.reason == f"limit reached: {RejectCode.DAILY_LOSS.value}"
        return False

    # ---------------------------------------------------------------- operator side
    def resume(self, operator: str, reason: str, state: AccountState, now: datetime | None = None) -> None:
        if not operator.strip() or len(reason.strip()) < 10:
            raise PermissionError("resuming needs an operator name and a written reason (10+ characters)")
        self.active_breaches |= {b.value for b in breached_limits(state, self.cfg)}
        if self.active_breaches:
            raise PermissionError("cannot resume while these are active: " + ", ".join(sorted(self.active_breaches)))
        if self.mode is Mode.EMERGENCY_FLATTEN:
            raise PermissionError("after an emergency flatten, review and restart the bot instead of resuming")
        self._set(Mode.RUNNING, operator, reason, now)

    def complete_drawdown_review(self, operator: str, reason: str, state: AccountState) -> None:
        """The drawdown breaker never clears by itself; an operator signs off a review first."""
        if not operator.strip() or len(reason.strip()) < 10:
            raise PermissionError("a drawdown review needs an operator name and a written reason")
        if state.drawdown_pct >= self.cfg.risk.drawdown_breaker_pct:
            raise PermissionError("drawdown is still at or beyond the breaker level")
        self.active_breaches.discard(RejectCode.DRAWDOWN.value)
        self.history.append(Transition(datetime.now(timezone.utc), self.mode, self.mode, operator,
                                       f"drawdown review completed: {reason}"))

    def block_entries(self, operator: str, reason: str, now: datetime | None = None) -> None:
        if self.mode in (Mode.RUNNING,):
            self._set(Mode.ENTRIES_BLOCKED, operator, reason, now)

    def pause(self, actor: str, reason: str, now: datetime | None = None) -> None:
        if self.mode is not Mode.EMERGENCY_FLATTEN:
            self._set(Mode.PAUSED, actor, reason, now)

    def emergency_flatten(self, operator: str, confirmation: str, now: datetime | None = None) -> None:
        if confirmation != EMERGENCY_CONFIRM_PHRASE:
            raise PermissionError(f'type "{EMERGENCY_CONFIRM_PHRASE}" exactly to flatten all positions')
        self._set(Mode.EMERGENCY_FLATTEN, operator, "operator ordered emergency flatten", now)

    @property
    def entries_allowed(self) -> bool:
        return self.mode is Mode.RUNNING and not self.active_breaches
