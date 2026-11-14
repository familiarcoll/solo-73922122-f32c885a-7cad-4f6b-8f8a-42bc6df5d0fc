"""Local-time classification for the fixed recorder timezone.

The tide recorder exports wall-clock time in ``America/New_York``.
Two wall-clock situations need special handling:

* a non-existent wall time (spring-forward gap) -> hard error;
* an ambiguous wall time (autumn fall-back, the repeated hour) -> the
  operator must choose whether the row belongs to the early (DST) or
  late (standard) hour.

PEP 495 gives naive datetimes a ``fold`` attribute.  Probing the two
folds against ``America/New_York`` gives three distinguishable cases
(verified empirically):

* **normal** — both folds resolve to the same UTC instant and round-trip
  back to the same wall clock;
* **ambiguous (repeated hour)** — the folds resolve to *different* UTC
  instants, but both round-trip back to this same wall clock;
* **gap (non-existent time)** — neither fold round-trips back to this
  wall clock; zoneinfo projects the time onto a neighbouring offset.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

TIMEZONE_NAME = "America/New_York"
NY = ZoneInfo(TIMEZONE_NAME)

# Fold 0 == the first (earlier) occurrence of a repeated hour.
EARLY = 0
# Fold 1 == the second (later) occurrence of a repeated hour.
LATE = 1


@dataclass(frozen=True)
class LocalTimeInfo:
    """Result of classifying a naive local wall-clock time."""

    kind: str  # "normal" | "gap" | "ambiguous"
    utc_early: datetime  # UTC instant computed with fold=0
    utc_late: datetime  # UTC instant computed with fold=1 (== early for gaps)


def _to_utc(naive: datetime, fold: int) -> datetime:
    aware = naive.replace(tzinfo=NY, fold=fold)
    return aware.astimezone(timezone.utc)


def classify_local(naive: datetime) -> LocalTimeInfo:
    """Classify a naive NY wall-clock datetime and compute its UTC forms."""
    if naive.tzinfo is not None:
        raise ValueError("classify_local expects a naive datetime")

    utc_early = _to_utc(naive, EARLY)
    utc_late = _to_utc(naive, LATE)
    wall_early = utc_early.astimezone(NY).replace(tzinfo=None)
    wall_late = utc_late.astimezone(NY).replace(tzinfo=None)

    if utc_early != utc_late:
        # Two distinct UTC instants resolve to this wall clock:
        # repeated hour when both round-trip, otherwise a spring gap.
        if wall_early == naive and wall_late == naive:
            kind = "ambiguous"
        else:
            kind = "gap"
    elif wall_early != naive:
        # Same UTC instant both ways but the wall clock differs: gap.
        kind = "gap"
    else:
        kind = "normal"

    return LocalTimeInfo(kind=kind, utc_early=utc_early, utc_late=utc_late)


def to_utc(naive: datetime, fold: int) -> datetime:
    """Convert a naive NY wall-clock time to UTC with an explicit fold."""
    return _to_utc(naive, 0 if fold == EARLY else LATE)
