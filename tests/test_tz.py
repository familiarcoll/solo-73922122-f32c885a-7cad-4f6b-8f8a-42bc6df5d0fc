from datetime import datetime

from app.tz import LATE, NY, classify_local, to_utc


def test_normal_time_round_trips_with_identical_folds():
    dt = datetime(2024, 7, 15, 8, 0, 0)
    info = classify_local(dt)
    assert info.kind == "normal"
    assert info.utc_early == info.utc_late
    # July is EDT (UTC-4): 08:00 local -> 12:00 UTC.
    assert info.utc_early.strftime("%Y-%m-%dT%H:%M:%SZ") == "2024-07-15T12:00:00Z"


def test_spring_forward_gap_is_detected():
    # 2024-03-10 02:00-03:00 does not exist in America/New_York.
    dt = datetime(2024, 3, 10, 2, 30, 0)
    info = classify_local(dt)
    assert info.kind == "gap"
    # Neither fold round-trips to the original wall clock, so the two
    # projected instants both land outside the skipped hour.
    assert info.utc_early.astimezone(NY).replace(tzinfo=None) != dt
    assert info.utc_late.astimezone(NY).replace(tzinfo=None) != dt


def test_fall_back_hour_is_ambiguous_and_folds_differ():
    # 2024-11-03 01:30 happens twice: EDT (UTC-4) then EST (UTC-5).
    dt = datetime(2024, 11, 3, 1, 30, 0)
    info = classify_local(dt)
    assert info.kind == "ambiguous"
    assert info.utc_early != info.utc_late
    assert info.utc_early.strftime("%Y-%m-%dT%H:%M:%SZ") == "2024-11-03T05:30:00Z"
    assert info.utc_late.strftime("%Y-%m-%dT%H:%M:%SZ") == "2024-11-03T06:30:00Z"


def test_to_utc_explicit_fold():
    dt = datetime(2024, 11, 3, 1, 30, 0)
    early = to_utc(dt, 0)
    late = to_utc(dt, LATE)
    assert (late - early).total_seconds() == 3600
