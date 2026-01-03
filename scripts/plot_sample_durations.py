#!/usr/bin/env python3
"""
Quick plot of per-sample processing durations for each tank.

Reads data/sample_process_time.csv produced by the tank controller timing
instrumentation and writes data/sample_process_time.png with two lines:
brookside and roadside duration (seconds) versus sample timestamp.
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

import matplotlib.pyplot as plt


LOG_PATH = Path(__file__).resolve().parents[1] / "data" / "sample_process_time.csv"
OUT_PATH = LOG_PATH.with_suffix(".png")


def parse_iso(ts: str) -> datetime:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def load_durations(path: Path) -> Dict[str, Tuple[List[datetime], List[float], List[float], List[float], List[float], List[float]]]:
    series: Dict[str, Tuple[List[datetime], List[float], List[float], List[float], List[float], List[float]]] = {}
    if not path.exists():
        raise FileNotFoundError(f"Timing log not found: {path}")
    with path.open() as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            tank = row.get("tank_id")
            ts = parse_iso(row.get("source_timestamp") or "")
            try:
                dur = float(row.get("duration_seconds") or 0.0)
            except ValueError:
                continue
            def _float(val):
                try:
                    return float(val)
                except Exception:
                    return None
            window_val = _float(row.get("window_minutes")) if row.get("window_minutes") else None
            if not tank or ts is None:
                continue
            hampel = _float(row.get("hampel_seconds"))
            flow = _float(row.get("flow_seconds"))
            short_flow = _float(row.get("short_flow_seconds"))
            main_flow = _float(row.get("main_flow_seconds"))
            series.setdefault(tank, ([], [], [], [], [], []))
            series[tank][0].append(ts)
            series[tank][1].append(dur)
            series[tank][2].append(window_val if window_val is not None else float("nan"))
            series[tank][3].append(hampel if hampel is not None else float("nan"))
            series[tank][4].append(short_flow if short_flow is not None else float("nan"))
            series[tank][5].append(main_flow if main_flow is not None else float("nan"))
    return series


def main() -> None:
    data = load_durations(LOG_PATH)
    fig, ax1 = plt.subplots(figsize=(11, 5))
    ax2 = ax1.twinx()
    for tank in ("brookside", "roadside"):
        if tank not in data:
            continue
        times, durations, windows, hampel, short_flow, main_flow = data[tank]
        if not times:
            continue
        ax1.plot(times, durations, label=f"{tank} duration")
        ax2.plot(times, windows, linestyle="--", alpha=0.4, label=f"{tank} window (min)")
        ax2.plot(times, hampel, linestyle=":", alpha=0.3, label=f"{tank} hampel (s)")
        ax2.plot(times, short_flow, linestyle="-.", alpha=0.3, label=f"{tank} short_flow (s)")
        ax2.plot(times, main_flow, linestyle="solid", alpha=0.3, label=f"{tank} main_flow (s)")
    ax1.set_xlabel("Sample timestamp")
    ax1.set_ylabel("Duration (seconds)")
    ax2.set_ylabel("Flow window (minutes)")
    ax1.set_title("Per-sample processing time")
    lines, labels = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines + lines2, labels + labels2, loc="upper left")
    fig.tight_layout()
    fig.autofmt_xdate()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    plt.savefig(OUT_PATH, dpi=150)
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
