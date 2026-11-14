from app.parser import parse_csv


def test_clean_sample_pipeline(sample_dir):
    content = (sample_dir / "sample_clean.csv").read_bytes()
    result = parse_csv(content, "sample_clean.csv")
    assert result.errors == []
    # 10 data lines; one lower revision superseded, one exact duplicate folded.
    assert len(result.kept_rows) == 8
    assert len(result.superseded_rows) == 1
    assert result.superseded_rows[0].revision == 1
    assert result.duplicate_lines == [3]

    by_source = {r.source_record_id: r for r in result.kept_rows}
    # Highest revision wins.
    assert by_source["SR-2002"].revision == 2
    assert by_source["SR-2002"].water_level == 1.02

    # Repeated-hour rows are ambiguous; ordinary rows resolve immediately.
    assert by_source["SR-1004"].time_kind == "ambiguous"
    assert by_source["SR-1004"].utc_time is None
    assert by_source["SR-1001"].time_kind == "normal"
    assert by_source["SR-1001"].utc_time is not None


def test_problem_sample_collects_all_error_classes(sample_dir):
    content = (sample_dir / "sample_problems.csv").read_bytes()
    result = parse_csv(content, "sample_problems.csv")
    joined = " | ".join(result.errors)
    assert "不存在" in joined          # spring-forward gap
    assert "不是数字" in joined        # non-numeric water level
    assert "有限数值" in joined        # NaN rejected
    assert "列数为 6" in joined        # extra column
    # Ambiguous rows still surface for operator choice.
    ambiguous = [r for r in result.kept_rows if r.time_kind == "ambiguous"]
    assert {r.source_record_id for r in ambiguous} == {"SR-9002", "SR-9003", "SR-8001"}
    # Lower revision of SR-8001 is dropped.
    assert {r.source_record_id for r in result.superseded_rows} == {"SR-8001"}


def test_same_revision_different_content_is_error():
    csv_text = (
        "device_id,source_record_id,revision,local_time,water_level\n"
        "D1,S1,3,2024-07-01 00:00:00,1.0\n"
        "D1,S1,3,2024-07-01 00:00:00,1.5\n"
    )
    result = parse_csv(csv_text.encode())
    assert len(result.errors) == 1
    assert "内容不一致" in result.errors[0]
    # The first occurrence remains the representative kept row.
    assert len(result.kept_rows) == 1
    assert result.kept_rows[0].water_level == 1.0


def test_identical_revision_repeats_are_folded():
    csv_text = (
        "device_id,source_record_id,revision,local_time,water_level\n"
        "D1,S1,3,2024-07-01 00:00:00,1.0\n"
        "D1,S1,3,2024-07-01 00:00:00,1.0\n"
    )
    result = parse_csv(csv_text.encode())
    assert result.errors == []
    assert len(result.kept_rows) == 1
    assert result.duplicate_lines == [3]


def test_bad_header_and_empty_file():
    assert parse_csv(b"").errors  # empty
    assert "只有表头" in parse_csv(
        b"device_id,source_record_id,revision,local_time,water_level\n"
    ).errors[0]
    bad = b"a,b,c,d,e\n1,2,3,4,5"
    assert "未知列名" in parse_csv(bad).errors[0]


def test_timezone_aware_timestamp_rejected():
    csv_text = (
        "device_id,source_record_id,revision,local_time,water_level\n"
        "D1,S1,1,2024-07-01T00:00:00-04:00,1.0\n"
    )
    result = parse_csv(csv_text.encode())
    assert "时区信息" in result.errors[0]
