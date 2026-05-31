"""Property tests for ingestion and lakehouse modules.

One design property per test, >=100 iterations, tagged. Library: Hypothesis.
"""

from __future__ import annotations

import numpy as np
from hypothesis import given, settings, strategies as st

from bbin_platform.ingestion import (
    BBox,
    QCThresholds,
    Sample,
    aggregate_15min,
    forecast_cycle_completes,
    identify_lag,
    imerg_pixels_in_basin,
    qc_check,
    samples_in_window,
    screen_snow_feature,
)
from bbin_platform.lakehouse import (
    AtcRevision,
    BronzeRecord,
    BronzeStore,
    FeatureInput,
    RatingCurveHistory,
    RatingCurveVersion,
    assemble_leakage_safe,
    effective_discharge,
    generation_mw,
    governing_for_block,
    lineage_is_complete,
)
from bbin_platform.schemas import LineageId

RUN = settings(max_examples=200, deadline=None)


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 23: Quality-control flags fire
# exactly on threshold violations.
# ---------------------------------------------------------------------------
@RUN
@given(
    reading=st.floats(-10, 110, allow_nan=False),
    last=st.floats(-10, 110, allow_nan=False),
    neighbor=st.floats(-10, 110, allow_nan=False),
)
def test_property_23_qc_flags(reading, last, neighbor):
    th = QCThresholds(min_stage=0, max_stage=100, max_rate_of_change=5,
                      frozen_repeat_count=3, neighbor_max_delta=10)
    flags = qc_check(reading, [last], [neighbor], th)
    assert flags.out_of_range == (reading < 0 or reading > 100)
    assert flags.rate_of_change == (abs(reading - last) > 5)
    assert flags.neighbor_inconsistent == (abs(reading - neighbor) > 10)


@RUN
@given(value=st.floats(0, 100, allow_nan=False))
def test_property_23_frozen_sensor(value):
    th = QCThresholds(0, 100, 1e9, frozen_repeat_count=3, neighbor_max_delta=1e9)
    # Two identical history + identical reading => frozen.
    flags = qc_check(value, [value, value], [], th)
    assert flags.frozen_sensor is True
    flags2 = qc_check(value, [value + 1, value], [], th)
    assert flags2.frozen_sensor is False


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 26: Telemetry aggregates exactly
# the samples in each 15-minute window.
# ---------------------------------------------------------------------------
@RUN
@given(
    offsets=st.lists(st.integers(0, 3600), max_size=40),
)
def test_property_26_window_aggregation(offsets):
    start = 1_000_000
    samples = [Sample(start + o, float(o)) for o in offsets]
    out = aggregate_15min(samples, start)
    # Each emitted window's count equals the independent count of samples in it.
    for w in out:
        expected = samples_in_window(samples, w["block_start_s"])
        assert w["count"] == expected
    # Total aggregated equals total in-range samples.
    assert sum(w["count"] for w in out) == len([s for s in samples if s.ts_epoch_s >= start])


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 27: IMERG subsetting selects
# exactly the intersecting 0.1-degree pixels.
# ---------------------------------------------------------------------------
@RUN
@given(
    lon0=st.floats(80.0, 88.0, allow_nan=False),
    lat0=st.floats(26.0, 30.0, allow_nan=False),
    w=st.floats(0.05, 1.0, allow_nan=False),
    h=st.floats(0.05, 1.0, allow_nan=False),
)
def test_property_27_imerg_subset(lon0, lat0, w, h):
    basin = BBox(lon0, lat0, lon0 + w, lat0 + h)
    cells = imerg_pixels_in_basin(basin, res_deg=0.1)
    res = 0.1
    # Every returned cell intersects the basin; brute-force check the bounding set.
    for (i, j) in cells:
        cell_min_lon = -180.0 + i * res
        cell_min_lat = -90.0 + j * res
        cell_max_lon = cell_min_lon + res
        cell_max_lat = cell_min_lat + res
        assert cell_min_lon < basin.max_lon and cell_max_lon > basin.min_lon
        assert cell_min_lat < basin.max_lat and cell_max_lat > basin.min_lat
    # No duplicates.
    assert len(cells) == len(set(cells))


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 28: Cloud-screened snow features
# are flagged stale, never interpolated as observed.
# ---------------------------------------------------------------------------
@RUN
@given(cloud=st.floats(0, 1, allow_nan=False), snow=st.floats(0, 1, allow_nan=False))
def test_property_28_cloud_screen(cloud, snow):
    feat = screen_snow_feature(cloud, snow, reliability_threshold=0.6)
    if cloud > 0.6:
        assert feat.flag == "STALE_UNAVAILABLE"
        assert feat.value is None
    else:
        assert feat.flag == "OBSERVED"
        assert feat.value == snow


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 29: Sentinel unavailability does
# not halt the forecast cycle.
# ---------------------------------------------------------------------------
@RUN
@given(core_ready=st.booleans(), sentinel=st.booleans())
def test_property_29_sentinel_non_blocking(core_ready, sentinel):
    assert forecast_cycle_completes(core_ready, sentinel) == core_ready


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 30: Per-basin discharge lags are
# recovered by cross-correlation.
# ---------------------------------------------------------------------------
@RUN
@given(lag=st.integers(0, 8))
def test_property_30_lag_recovery(lag):
    rng = np.random.default_rng(12345 + lag)
    n = 200
    driver = rng.standard_normal(n + 8)
    response = np.empty(n + 8)
    response[:lag] = rng.standard_normal(lag) * 0.01
    response[lag:] = driver[: n + 8 - lag]  # response is driver delayed by `lag`
    recovered = identify_lag(driver, response, max_lag=8)
    assert recovered == lag


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 43: Bronze preserves raw records
# append-only with validation outcomes.
# ---------------------------------------------------------------------------
@RUN
@given(
    n_valid=st.integers(0, 15),
    n_invalid=st.integers(0, 10),
)
def test_property_43_bronze_append_only(n_valid, n_invalid):
    store = BronzeStore()
    for i in range(n_valid):
        store.append(BronzeRecord(f"v{i}", "bytes", "VALID", {"arr": i}))
    for i in range(n_invalid):
        store.append(BronzeRecord(f"x{i}", "bytes", "INVALID", {"arr": i}))
    assert len(store) == n_valid
    assert len(store.quarantine) == n_invalid
    # Correction is a distinct record, original retained.
    if n_valid > 0:
        store.append(BronzeRecord("corr0", "newbytes", "VALID", {}, is_correction=True,
                                  corrects_id="v0"))
        ids = {r.record_id for r in store.records}
        assert "v0" in ids and "corr0" in ids


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 24: Discharge conversion uses the
# rating-curve version effective at observation time.
# ---------------------------------------------------------------------------
@RUN
@given(stage=st.floats(0.1, 10, allow_nan=False), which=st.sampled_from(["v1", "v2"]))
def test_property_24_rating_curve_version(stage, which):
    v1 = RatingCurveVersion("v1", "2025-01-01T00:00:00Z", "2026-01-01T00:00:00Z", 2.0, 1.5)
    v2 = RatingCurveVersion("v2", "2026-01-01T00:00:00Z", "2027-01-01T00:00:00Z", 3.0, 1.6)
    hist = RatingCurveHistory([v1, v2])
    when = "2025-06-01T00:00:00Z" if which == "v1" else "2026-06-01T00:00:00Z"
    res = hist.convert(stage, when)
    assert res.rating_curve_version == which
    expected = (v1 if which == "v1" else v2).discharge(stage)
    assert abs(res.discharge_m3s - expected) < 1e-9


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 25: Physical caps and
# non-negativity hold for derived generation.
# ---------------------------------------------------------------------------
@RUN
@given(
    raw_q=st.floats(-50, 500, allow_nan=False),
    design=st.floats(1, 200, allow_nan=False),
    head=st.floats(-5, 300, allow_nan=False),
    eff=st.floats(0, 1, allow_nan=False),
    cap=st.floats(1, 1000, allow_nan=False),
)
def test_property_25_physical_caps(raw_q, design, head, eff, cap):
    eq = effective_discharge(raw_q, design)
    assert 0.0 <= eq <= design
    gen = generation_mw(eq, head, eff, cap)
    assert 0.0 <= gen <= cap


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 11: ATC revisions affect only
# future blocks.
# ---------------------------------------------------------------------------
@RUN
@given(block_day=st.integers(1, 10))
def test_property_11_atc_revision_future_only(block_day):
    base = AtcRevision(100.0, "2026-06-01T00:00:00Z", 1)
    rev = AtcRevision(60.0, "2026-06-05T00:00:00Z", 2)
    block = f"2026-06-{block_day:02d}T00:00:00Z"
    g = governing_for_block(block, base, rev)
    if block_day >= 5:
        assert g == 60.0
    else:
        assert g == 100.0


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 31: Features at bid time are
# leakage-safe.
# ---------------------------------------------------------------------------
@RUN
@given(
    avail_before=st.booleans(),
    same_block_price=st.booleans(),
    final_run=st.booleans(),
)
def test_property_31_leakage_safe(avail_before, same_block_price, final_run):
    bid = "2026-06-01T12:00:00Z"
    avail = "2026-06-01T11:00:00Z" if avail_before else "2026-06-01T13:00:00Z"
    f = FeatureInput("feat", avail, same_block_price, final_run)
    safe = assemble_leakage_safe([f], bid, is_operational_backtest=True)
    should_keep = avail_before and not same_block_price and not final_run
    assert (len(safe) == 1) == should_keep


# ---------------------------------------------------------------------------
# Feature: bbin-hydropower-platform, Property 52: Every Gold output carries
# complete, valid lineage and evidence.
# ---------------------------------------------------------------------------
@RUN
@given(
    has_lineage_id=st.booleans(),
    transacted=st.booleans(),
    has_human_event=st.booleans(),
)
def test_property_52_lineage_complete(has_lineage_id, transacted, has_human_event):
    lin = LineageId(
        lineage_id="L1" if has_lineage_id else "",
        source_event_ids=["e1"], external_file_sha256=["h1"],
        bronze_table_versions=["b1"], silver_table_versions=["s1"],
        feature_definition_version="fv1", model_version="mv1",
        calibration_window="2026Q2", random_seed=7,
        regulatory_ruleset_id="rs1", approval_ids=["a1"],
        atc_declaration_revision=2, code_commit="abc123",
        container_digest="sha256:deadbeef",
        human_approval_event_id="he1" if has_human_event else None,
    )
    complete = lineage_is_complete(lin, transacted)
    expected = has_lineage_id and (not transacted or has_human_event)
    assert complete == expected
