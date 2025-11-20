"""
Synthetic clock + CSV replay helpers for debug/testing.

The Tank Pi uses this module when DEBUG_TANK or DEBUG_RELEASER is enabled.
"""
from __future__ import annotations

import csv
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Sequence


def parse_timestamp(value: str) -> datetime:
    """Parse either ISO timestamps or `%Y-%m-%d %H:%M:%S` CSV entries."""
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d-%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d-%H:%M:%S",
    ):
        try:
            parsed = datetime.strptime(value, fmt)
            return parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    # Fall back to auto parser
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


@dataclass
class SyntheticClock:
    """Wall-clock-backed simulated time that can run faster than real time."""

    start_timestamp: datetime
    multiplier: float = 2.0

    def __post_init__(self) -> None:
        if self.multiplier <= 0:
            raise ValueError("multiplier must be > 0")
        self._real_start = datetime.now(timezone.utc)

    def now(self) -> datetime:
        real_delta = datetime.now(timezone.utc) - self._real_start
        synth_delta = real_delta.total_seconds() * self.multiplier
        return self.start_timestamp + timedelta(seconds=synth_delta)

    def wait_until(self, target: datetime, poll_seconds: float = 0.25) -> None:
        """Sleep until the synthetic clock reaches the target timestamp."""
        while self.now() < target:
            time.sleep(poll_seconds)


@dataclass
class CsvRecord:
    timestamp: datetime
    raw: Dict[str, str]


def load_csv_records(paths: Iterable[Path]) -> List[CsvRecord]:
    records: List[CsvRecord] = []
    for path in paths:
        if not path or not path.exists():
            continue
        with path.open() as fp:
            reader = csv.DictReader(fp)
            for row in reader:
                ts_field = row.get("timestamp") or row.get("Time")
                if not ts_field:
                    continue
                records.append(CsvRecord(timestamp=parse_timestamp(ts_field), raw=row))
    records.sort(key=lambda r: r.timestamp)
    return records


class CsvReplay:
    """Iterator that yields CSV rows when synthetic time reaches their stamp."""

    def __init__(self, records: List[CsvRecord], clock: SyntheticClock):
        self.records = records
        self.clock = clock
        self._index = 0

    def __iter__(self) -> Iterator[CsvRecord]:
        return self

    def __next__(self) -> CsvRecord:
        if self._index >= len(self.records):
            raise StopIteration
        record = self.records[self._index]
        self.clock.wait_until(record.timestamp)
        self._index += 1
        return record


def start_from_records(records: Sequence[CsvRecord]) -> datetime:
    """Return the earliest timestamp in the sample set or now() if empty."""
    if not records:
        return datetime.now(timezone.utc)
    return min(record.timestamp for record in records)
