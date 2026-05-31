"""Thread-safe in-memory state for the running service.

Holds the latest cycle result per plant, a bounded history, and a maker-checker
approval log. In production this would be backed by the lakehouse and an
append-only store; here it is an in-process store guarded by a lock.
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Any, Optional


class ServiceState:
    def __init__(self, history_per_plant: int = 50) -> None:
        self._lock = threading.RLock()
        self._latest: dict[str, dict[str, Any]] = {}
        self._history: dict[str, deque] = {}
        self._approvals: dict[str, dict[str, Any]] = {}
        self._history_per_plant = history_per_plant
        self.started_at: Optional[str] = None
        self.cycles_run = 0

    def record_cycle(self, result: dict[str, Any]) -> None:
        with self._lock:
            plant = result["plant_id"]
            self._latest[plant] = result
            self._history.setdefault(plant, deque(maxlen=self._history_per_plant))
            self._history[plant].append({
                "cycle_id": result["cycle_id"],
                "generated_at_utc": result["generated_at_utc"],
                "recommended_mw": result["decision"]["recommended_mw"],
                "limiting_constraint": result["decision"]["limiting_constraint"],
                "order_emitted": result["decision"]["order_emitted"],
            })
            self.cycles_run += 1

    def latest(self, plant: str) -> Optional[dict[str, Any]]:
        with self._lock:
            return self._latest.get(plant)

    def all_latest(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._latest.values())

    def history(self, plant: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._history.get(plant, []))

    def record_approval(self, cycle_id: str, actor: str, role: str) -> dict[str, Any]:
        """Record a maker or checker acceptance for a cycle's recommendation.

        Enforces separation of duties: the checker must differ from the maker.
        Returns the current approval record for the cycle.
        """
        with self._lock:
            rec = self._approvals.setdefault(
                cycle_id, {"cycle_id": cycle_id, "maker": None, "checker": None,
                           "transmitted": False})
            if role == "maker":
                rec["maker"] = actor
            elif role == "checker":
                if rec.get("maker") == actor:
                    raise ValueError("checker must differ from maker (separation of duties)")
                rec["checker"] = actor
            else:
                raise ValueError(f"unknown role {role!r}; expected 'maker' or 'checker'")
            if rec["maker"] and rec["checker"] and rec["maker"] != rec["checker"]:
                rec["transmitted"] = True
            return dict(rec)

    def approval(self, cycle_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            rec = self._approvals.get(cycle_id)
            return dict(rec) if rec else None
