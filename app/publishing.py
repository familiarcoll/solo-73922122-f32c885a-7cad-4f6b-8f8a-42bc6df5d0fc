"""UTC conflict detection and published CSV rendering."""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from .models import KeptRow, ParseResult
from .tz import LATE, to_utc


@dataclass
class Conflict:
    """Two different source records of one device sharing one UTC instant."""

    device_id: str
    utc_text: str
    rows: list[KeptRow]


def apply_choice(row: KeptRow, choice: str) -> str | None:
    """Apply an early/late choice to an ambiguous row.

    Returns an error message when the choice is invalid, else None.
    """
    if row.time_kind != "ambiguous":
        return f"第 {row.line_no} 行不是重复时段，无需选择"
    if choice not in ("early", "late"):
        return f"第 {row.line_no} 行: 选择必须是 early 或 late，收到 {choice!r}"
    fold = LATE if choice == "late" else 0
    row.fold_choice = choice
    row.utc_time = to_utc(row.local_time, fold)
    return None


def unresolved_ambiguous(result: ParseResult) -> list[KeptRow]:
    return [r for r in result.kept_rows if r.time_kind == "ambiguous"
            and r.fold_choice is None]


def find_conflicts(result: ParseResult) -> list[Conflict]:
    """Conflicts only count when every involved row has a UTC instant."""
    bucket: dict[tuple[str, str], list[KeptRow]] = {}
    for row in result.kept_rows:
        if row.utc_time is None:
            continue
        utc_text = _format_utc(row.utc_time)
        bucket.setdefault((row.device_id, utc_text), []).append(row)

    conflicts: list[Conflict] = []
    for (device_id, utc_text), rows in sorted(bucket.items()):
        source_ids = {r.source_record_id for r in rows}
        if len(source_ids) > 1:
            conflicts.append(Conflict(device_id=device_id,
                                      utc_text=utc_text,
                                      rows=sorted(rows, key=lambda r: r.line_no)))
    return conflicts


def is_publishable(result: ParseResult) -> tuple[bool, list[str]]:
    """Return (ok, blocking reasons)."""
    reasons: list[str] = list(result.errors)
    reasons.extend(f"第 {r.line_no} 行: 重复时段尚未选择早/晚"
                   for r in unresolved_ambiguous(result))
    for conflict in find_conflicts(result):
        lines = ", ".join(
            f"第 {r.line_no} 行(源记录 {r.source_record_id})" for r in conflict.rows
        )
        reasons.append(
            f"设备 {conflict.device_id} 在 UTC {conflict.utc_text} "
            f"存在不同源记录冲突: {lines}"
        )
    return (not reasons), reasons


def _format_utc(dt) -> str:
    # Always naive-style ISO UTC text with explicit suffix for clarity.
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def render_utc_csv(result: ParseResult) -> str:
    """Render the final UTC CSV. Caller must ensure publishability."""
    ok, reasons = is_publishable(result)
    if not ok:
        raise ValueError("存在未解决的问题，不能发布: " + "; ".join(reasons))

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["device_id", "source_record_id", "revision",
                     "utc_time", "water_level"])
    rows = sorted(result.kept_rows,
                  key=lambda r: (r.device_id, r.utc_time, r.source_record_id))
    for row in rows:
        writer.writerow([
            row.device_id,
            row.source_record_id,
            row.revision,
            _format_utc(row.utc_time),
            row.water_level,
        ])
    return buffer.getvalue()
