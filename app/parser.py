"""CSV parsing and the full validation pipeline.

Pipeline order (a failure at an early stage is recorded and the row is
skipped so that the operator sees as many problems as possible in one
upload):

1. structural parsing: 5 columns, integer revision, finite water level,
   parseable naive local timestamp;
2. revision handling per (device, source record):
     - identical repeats collapse to one line,
     - same revision with different content is a hard error,
     - only the highest revision survives;
3. local-time classification (normal / spring-forward gap / repeated
   hour); gap is a hard error, repeated hour awaits an operator choice;
4. UTC conflict detection: after choices, the same device sharing one
   UTC instant across different source records blocks publication.
"""

from __future__ import annotations

import csv
import io
import math
from datetime import datetime

from .models import KeptRow, ParseResult, RawRow
from .tz import classify_local

EXPECTED_HEADERS = [
    "device_id",
    "source_record_id",
    "revision",
    "local_time",
    "water_level",
]

# Accept a few common header spellings, including Chinese column names.
HEADER_ALIASES = {
    "device_id": "device_id",
    "device": "device_id",
    "设备号": "device_id",
    "source_record_id": "source_record_id",
    "source_record": "source_record_id",
    "源记录号": "source_record_id",
    "revision": "revision",
    "rev": "revision",
    "修订号": "revision",
    "local_time": "local_time",
    "local_time_ny": "local_time",
    "本地时刻": "local_time",
    "water_level": "water_level",
    "level": "water_level",
    "水位": "water_level",
}

_TIMESTAMP_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%dT%H:%M",
)


def _parse_timestamp(text: str) -> datetime:
    """Parse a naive local timestamp; reject tz-aware strings."""
    text = text.strip()
    for fmt in _TIMESTAMP_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    # Last resort: ISO 8601 with fractional seconds.
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"无法解析的本地时刻 {text!r}") from exc
    if parsed.tzinfo is not None:
        raise ValueError(
            f"本地时刻 {text!r} 带有时区信息；本系统固定使用 America/New_York 本地时间"
        )
    return parsed.replace(microsecond=0)


def _normalize_headers(header: list[str]) -> list[str]:
    normalized: list[str] = []
    for name in header:
        key = (name or "").strip().lstrip("﻿").lower()
        canonical = HEADER_ALIASES.get(key)
        if canonical is None:
            raise ValueError(f"未知列名 {name!r}，期望列: {', '.join(EXPECTED_HEADERS)}")
        normalized.append(canonical)
    if sorted(normalized) != sorted(EXPECTED_HEADERS):
        raise ValueError(
            f"表头不完整或重复，期望列: {', '.join(EXPECTED_HEADERS)}，实际: {header}"
        )
    return normalized


def parse_csv(content: bytes, filename: str = "upload.csv") -> ParseResult:
    """Parse raw upload bytes and run the complete validation pipeline."""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = content.decode("gb18030")
        except UnicodeDecodeError as exc:
            return ParseResult(
                filename=filename,
                errors=[f"文件编码无法识别（需 UTF-8 或 GB18030）: {exc}"],
            )

    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows or not any(cell.strip() for cell in rows[0]):
        return ParseResult(filename=filename, errors=["文件为空或缺少表头"])

    try:
        columns = _normalize_headers(rows[0])
    except ValueError as exc:
        return ParseResult(filename=filename, errors=[str(exc)])

    idx = {name: columns.index(name) for name in EXPECTED_HEADERS}
    result = ParseResult(filename=filename)

    # key = (device, source record) -> revision -> representative RawRow.
    grouped: dict[tuple[str, str], dict[int, RawRow]] = {}

    data_line_count = 0
    for line_offset, cells in enumerate(rows[1:], start=2):
        if not cells or all(not c.strip() for c in cells):
            continue  # tolerate blank lines
        data_line_count += 1
        if len(cells) != len(EXPECTED_HEADERS):
            result.errors.append(
                f"第 {line_offset} 行: 列数为 {len(cells)}，应为 {len(EXPECTED_HEADERS)}"
            )
            continue

        device_id = cells[idx["device_id"]].strip()
        source_record_id = cells[idx["source_record_id"]].strip()
        rev_text = cells[idx["revision"]].strip()
        time_text = cells[idx["local_time"]].strip()
        level_text = cells[idx["water_level"]].strip()

        if not device_id or not source_record_id:
            result.errors.append(f"第 {line_offset} 行: 设备号和源记录号不能为空")
            continue
        try:
            revision = int(rev_text)
        except ValueError:
            result.errors.append(f"第 {line_offset} 行: 修订号 {rev_text!r} 不是整数")
            continue
        try:
            local_time = _parse_timestamp(time_text)
        except ValueError as exc:
            result.errors.append(f"第 {line_offset} 行: {exc}")
            continue
        try:
            water_level = float(level_text)
        except ValueError:
            result.errors.append(
                f"第 {line_offset} 行: 水位 {level_text!r} 不是数字"
            )
            continue
        if not math.isfinite(water_level):
            result.errors.append(
                f"第 {line_offset} 行: 水位必须为有限数值，收到 {level_text!r}"
            )
            continue

        raw = RawRow(
            line_no=line_offset,
            device_id=device_id,
            source_record_id=source_record_id,
            revision=revision,
            local_time=local_time,
            water_level=water_level,
            raw_local_text=time_text,
        )
        key = (device_id, source_record_id)
        rev_map = grouped.setdefault(key, {})
        existing = rev_map.get(revision)
        if existing is None:
            rev_map[revision] = raw
        elif _same_content(existing, raw):
            result.duplicate_lines.append(line_offset)
        else:
            result.errors.append(
                f"第 {line_offset} 行与第 {existing.line_no} 行: "
                f"设备 {device_id} 源记录 {source_record_id} 修订号 {revision} "
                "相同但内容不一致"
            )

    # Revision selection + time classification.
    if data_line_count == 0:
        result.errors.append("文件只有表头，没有任何数据行")

    for (device_id, source_id), rev_map in grouped.items():
        highest = max(rev_map)
        for revision, raw in rev_map.items():
            if revision != highest:
                result.superseded_rows.append(raw)
                continue
            info = classify_local(raw.local_time)
            kept = KeptRow(
                line_no=raw.line_no,
                device_id=raw.device_id,
                source_record_id=raw.source_record_id,
                revision=raw.revision,
                local_time=raw.local_time,
                raw_local_text=raw.raw_local_text,
                water_level=raw.water_level,
                time_kind=info.kind,
            )
            if info.kind == "gap":
                result.errors.append(
                    f"第 {raw.line_no} 行: 本地时刻 {raw.raw_local_text} "
                    "在 America/New_York 不存在（夏令时跳过的一小时）"
                )
            elif info.kind == "normal":
                kept.utc_time = info.utc_early
            # ambiguous rows stay unresolved until the operator chooses.
            result.kept_rows.append(kept)

    result.kept_rows.sort(key=lambda r: (r.device_id, r.line_no))
    return result


def _same_content(a: RawRow, b: RawRow) -> bool:
    return (
        a.local_time == b.local_time
        and a.water_level == b.water_level
        and a.raw_local_text == b.raw_local_text
    )
