"""Domain dataclasses for one upload's validation pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class RawRow:
    """One parsed CSV line, before revision/time processing."""

    line_no: int
    device_id: str
    source_record_id: str
    revision: int
    local_time: datetime  # naive NY wall clock
    water_level: float
    raw_local_text: str


@dataclass
class KeptRow:
    """A row that survives the highest-revision selection."""

    line_no: int
    device_id: str
    source_record_id: str
    revision: int
    local_time: datetime
    raw_local_text: str
    water_level: float
    time_kind: str  # "normal" | "ambiguous" | "gap"
    fold_choice: Optional[int] = None  # None until operator picks (ambiguous)
    utc_time: Optional[datetime] = None  # set once normal or chosen


@dataclass
class ParseResult:
    """Everything the review page needs for one uploaded file."""

    filename: str
    kept_rows: list[KeptRow] = field(default_factory=list)
    # Rows dropped because a higher revision of the same key exists.
    superseded_rows: list[RawRow] = field(default_factory=list)
    # Duplicate identical (device, source, revision) lines collapsed away.
    duplicate_lines: list[int] = field(default_factory=list)
    # Hard errors that make the file unpublishable. Human-readable, with line no.
    errors: list[str] = field(default_factory=list)
