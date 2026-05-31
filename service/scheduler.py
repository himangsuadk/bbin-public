"""Background forecast-cycle scheduler.

Runs a cycle for every configured plant on a fixed interval in a daemon thread,
writing each result into the shared ServiceState. The production target is a
900-second (15-minute) cadence; the interval is configurable so the demo can run
faster.
"""

from __future__ import annotations

import threading
import time
import traceback
from typing import Iterable

from .engine import DEFAULT_PLANTS, PlantConfig, run_cycle
from .state import ServiceState


class ForecastScheduler:
    def __init__(self, state: ServiceState, plants: Iterable[PlantConfig] = DEFAULT_PLANTS,
                 interval_s: float = 900.0, n_paths: int = 100_000) -> None:
        self._state = state
        self._plants = list(plants)
        self._interval_s = interval_s
        self._n_paths = n_paths
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _run_once(self) -> None:
        for cfg in self._plants:
            try:
                result = run_cycle(cfg, n_paths=self._n_paths)
                self._state.record_cycle(result.to_dict())
            except Exception:  # never let one plant kill the loop
                print(f"[scheduler] cycle failed for {cfg.plant_id}:\n{traceback.format_exc()}")

    def _loop(self) -> None:
        # Run immediately on start, then on the interval.
        self._run_once()
        while not self._stop.wait(self._interval_s):
            self._run_once()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="forecast-scheduler",
                                        daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5.0)
