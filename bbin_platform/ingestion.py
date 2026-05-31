"""Hydrology and Earth-observation ingestion logic (Task 11).

Pure logic for gauge QC, 15-minute telemetry aggregation, IMERG 0.1-degree
basin subsetting, Sentinel cloud screening, and discharge-lag identification.
The network/file retrieval (Earthdata, CDSE) is out of scope here; the
algorithms that operate on retrieved data are implemented and tested.

Properties: 23, 26, 27, 28, 29, 30
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

import numpy as np


# ---------------------------------------------------------------------------
# Gauge quality control (Property 23)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QCThresholds:
    min_stage: float
    max_stage: float
    max_rate_of_change: float       # per reading
    frozen_repeat_count: int        # identical consecutive readings => frozen
    neighbor_max_delta: float       # max |reading - neighbor_mean|


@dataclass
class QCFlags:
    out_of_range: bool = False
    rate_of_change: bool = False
    frozen_sensor: bool = False
    neighbor_inconsistent: bool = False

    @property
    def any_flag(self) -> bool:
        return (self.out_of_range or self.rate_of_change
                or self.frozen_sensor or self.neighbor_inconsistent)


def qc_check(reading: float, history: list[float], neighbors: list[float],
             th: QCThresholds) -> QCFlags:
    """Set each QC flag iff its specific threshold is violated (Property 23)."""
    flags = QCFlags()
    if reading < th.min_stage or reading > th.max_stage:
        flags.out_of_range = True
    if history:
        if abs(reading - history[-1]) > th.max_rate_of_change:
            flags.rate_of_change = True
    # Frozen: last (frozen_repeat_count-1) history values plus this reading identical.
    if th.frozen_repeat_count >= 2 and len(history) >= th.frozen_repeat_count - 1:
        window = history[-(th.frozen_repeat_count - 1):] + [reading]
        if len(window) == th.frozen_repeat_count and all(abs(v - reading) < 1e-12 for v in window):
            flags.frozen_sensor = True
    if neighbors:
        nmean = sum(neighbors) / len(neighbors)
        if abs(reading - nmean) > th.neighbor_max_delta:
            flags.neighbor_inconsistent = True
    return flags


# ---------------------------------------------------------------------------
# 15-minute telemetry aggregation (Property 26)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Sample:
    ts_epoch_s: int
    value: float


def aggregate_15min(samples: list[Sample], window_start_s: int) -> list[dict]:
    """Group samples into contiguous 900-second windows; each output aggregates
    exactly the samples whose timestamp falls in [start, start+900) (Property 26)."""
    WIN = 900
    buckets: dict[int, list[float]] = {}
    for s in samples:
        if s.ts_epoch_s < window_start_s:
            continue
        idx = (s.ts_epoch_s - window_start_s) // WIN
        buckets.setdefault(idx, []).append(s.value)
    out = []
    for idx in sorted(buckets):
        vals = buckets[idx]
        out.append({
            "block_start_s": window_start_s + idx * WIN,
            "count": len(vals),
            "mean": sum(vals) / len(vals),
        })
    return out


def samples_in_window(samples: list[Sample], start_s: int) -> int:
    return sum(1 for s in samples if start_s <= s.ts_epoch_s < start_s + 900)


# ---------------------------------------------------------------------------
# IMERG 0.1-degree basin subsetting (Property 27)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BBox:
    """Axis-aligned basin polygon proxy in lon/lat degrees."""
    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float


def imerg_pixels_in_basin(basin: BBox, res_deg: float = 0.1,
                          grid_origin_lon: float = -180.0,
                          grid_origin_lat: float = -90.0) -> list[tuple[int, int]]:
    """Return exactly the 0.1-degree grid cells that intersect the basin bbox
    (Property 27). Cell (i, j) spans [origin + i*res, origin + (i+1)*res)."""
    def cell_range(lo, hi, origin):
        i0 = math.floor((lo - origin) / res_deg)
        i1 = math.floor((hi - origin) / res_deg - 1e-12)
        return i0, i1

    i0, i1 = cell_range(basin.min_lon, basin.max_lon, grid_origin_lon)
    j0, j1 = cell_range(basin.min_lat, basin.max_lat, grid_origin_lat)
    cells = []
    for i in range(i0, i1 + 1):
        for j in range(j0, j1 + 1):
            cells.append((i, j))
    return cells


# ---------------------------------------------------------------------------
# Sentinel cloud screening (Property 28)
# ---------------------------------------------------------------------------


@dataclass
class SnowFeature:
    value: Optional[float]
    flag: str  # "OBSERVED" | "STALE_UNAVAILABLE"


def screen_snow_feature(cloud_fraction: float, raw_snow_index: float,
                        reliability_threshold: float = 0.6) -> SnowFeature:
    """If cloud fraction exceeds the reliability threshold, flag the snow feature
    stale/unavailable rather than interpolating it as observed (Property 28)."""
    if cloud_fraction > reliability_threshold:
        return SnowFeature(value=None, flag="STALE_UNAVAILABLE")
    return SnowFeature(value=raw_snow_index, flag="OBSERVED")


# ---------------------------------------------------------------------------
# Forecast cycle non-blocking on Sentinel (Property 29)
# ---------------------------------------------------------------------------


def forecast_cycle_completes(core_inputs_ready: bool,
                             sentinel_available: bool) -> bool:
    """Sentinel features are non-blocking auxiliary inputs; the cycle completes
    as long as core inputs are ready, regardless of Sentinel availability
    (Property 29)."""
    return core_inputs_ready


# ---------------------------------------------------------------------------
# Discharge-lag identification by cross-correlation (Property 30)
# ---------------------------------------------------------------------------


def identify_lag(driver: np.ndarray, response: np.ndarray, max_lag: int) -> int:
    """Recover the lag L (0 <= L <= max_lag) that maximizes cross-correlation
    between a driver series and a lagged response (Property 30)."""
    best_lag, best_corr = 0, -np.inf
    for lag in range(max_lag + 1):
        if lag >= len(driver):
            break
        d = driver[: len(driver) - lag]
        r = response[lag:]
        if len(d) < 2:
            break
        d = d - d.mean()
        r = r - r.mean()
        denom = (np.linalg.norm(d) * np.linalg.norm(r))
        if denom == 0:
            corr = -np.inf
        else:
            corr = float(np.dot(d, r) / denom)
        if corr > best_corr:
            best_corr, best_lag = corr, lag
    return best_lag
