"""Global kill switch.

Active when EITHER the ``KILL_SWITCH`` env flag is true OR it was engaged via
``POST /kill``. The runtime flag is stored in the database, so it survives
restarts. ``POST /resume`` only clears the runtime flag; the env flag can only
be cleared by changing the environment and restarting.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from app.db import utcnow
from app.store import Store, iso

STATE_KEY = "kill_switch"


@dataclass(frozen=True)
class KillSwitchStatus:
    active: bool
    env_flag: bool
    runtime_flag: bool
    reason: str | None
    since: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class KillSwitch:
    def __init__(self, store: Store, env_flag: bool):
        self._store = store
        self._env_flag = env_flag

    def status(self) -> KillSwitchStatus:
        state = self._store.get_state(STATE_KEY) or {}
        runtime = bool(state.get("active"))
        reason = state.get("reason") if runtime else None
        if self._env_flag and not reason:
            reason = "KILL_SWITCH env flag is set"
        return KillSwitchStatus(
            active=self._env_flag or runtime,
            env_flag=self._env_flag,
            runtime_flag=runtime,
            reason=reason,
            since=state.get("since") if runtime else None,
        )

    def engage(self, reason: str) -> KillSwitchStatus:
        self._store.set_state(STATE_KEY, {"active": True, "reason": reason, "since": iso(utcnow())})
        return self.status()

    def release(self) -> KillSwitchStatus:
        self._store.set_state(STATE_KEY, {"active": False, "reason": None, "since": None})
        return self.status()
