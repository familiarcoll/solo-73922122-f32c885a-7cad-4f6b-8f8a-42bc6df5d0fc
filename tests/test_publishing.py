import pytest

from app.parser import parse_csv
from app.publishing import (
    apply_choice,
    find_conflicts,
    is_publishable,
    render_utc_csv,
    unresolved_ambiguous,
)


def _row(result, source_id):
    return next(r for r in result.kept_rows if r.source_record_id == source_id)


def test_choices_resolve_ambiguous_rows():
    csv_text = (
        "device_id,source_record_id,revision,local_time,water_level\n"
        "D1,S1,1,2024-11-03 01:30:00,1.0\n"
        "D1,S2,1,2024-11-03 01:30:00,2.0\n"
    )
    result = parse_csv(csv_text.encode())
    assert len(unresolved_ambiguous(result)) == 2
    ok, reasons = is_publishable(result)
    assert not ok

    assert apply_choice(_row(result, "S1"), "early") is None
    assert apply_choice(_row(result, "S2"), "late") is None
    assert unresolved_ambiguous(result) == []
    assert find_conflicts(result) == []
    ok, _ = is_publishable(result)
    assert ok

    # Early = 05:30Z, late = 06:30Z.
    assert _row(result, "S1").utc_time.strftime("%H:%M") == "05:30"
    assert _row(result, "S2").utc_time.strftime("%H:%M") == "06:30"


def test_same_utc_different_sources_blocks_publication():
    csv_text = (
        "device_id,source_record_id,revision,local_time,water_level\n"
        "D1,S1,1,2024-11-03 01:30:00,1.0\n"
        "D1,S2,1,2024-11-03 01:30:00,2.0\n"
    )
    result = parse_csv(csv_text.encode())
    apply_choice(_row(result, "S1"), "early")
    apply_choice(_row(result, "S2"), "early")  # same UTC instant
    conflicts = find_conflicts(result)
    assert len(conflicts) == 1
    assert {r.source_record_id for r in conflicts[0].rows} == {"S1", "S2"}
    ok, reasons = is_publishable(result)
    assert not ok and any("冲突" in r for r in reasons)

    with pytest.raises(ValueError):
        render_utc_csv(result)


def test_different_devices_sharing_utc_are_not_conflicts():
    csv_text = (
        "device_id,source_record_id,revision,local_time,water_level\n"
        "D1,S1,1,2024-07-01 00:00:00,1.0\n"
        "D2,S9,1,2024-07-01 00:00:00,2.0\n"
    )
    result = parse_csv(csv_text.encode())
    assert find_conflicts(result) == []
    ok, _ = is_publishable(result)
    assert ok


def test_render_utc_csv_format_and_order():
    csv_text = (
        "device_id,source_record_id,revision,local_time,water_level\n"
        "D1,S2,1,2024-07-01 04:00:00,2.0\n"
        "D1,S1,1,2024-07-01 00:00:00,1.0\n"
    )
    result = parse_csv(csv_text.encode())
    text = render_utc_csv(result)
    lines = text.strip().splitlines()
    assert lines[0] == "device_id,source_record_id,revision,utc_time,water_level"
    # Sorted by UTC: 00:00 EDT -> 04:00Z comes first.
    assert lines[1].endswith("2024-07-01T04:00:00Z,1.0")
    assert lines[2].endswith("2024-07-01T08:00:00Z,2.0")


def test_invalid_choice_rejected():
    csv_text = (
        "device_id,source_record_id,revision,local_time,water_level\n"
        "D1,S1,1,2024-11-03 01:30:00,1.0\n"
    )
    result = parse_csv(csv_text.encode())
    assert apply_choice(_row(result, "S1"), "noon") is not None
