#!/usr/bin/env python3
"""
Tank Pi orchestrator.

For now the focus is on debug replay: we stream historical CSV data with a
SyntheticClock, continuously regenerate web/data/status.json, and serve the
front-end assets on a local HTTP port so the UI can be exercised without
physical sensors.
"""
from __future__ import annotations

import csv
import json
import logging
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from config_loader import load_role, repo_path_from_config
from synthetic_clock import SyntheticClock, parse_timestamp


LOGGER = logging.getLogger("tank_pi")


@dataclass
class Event:
    timestamp: datetime
    kind: str  # "tank" or "pump"
    data: Dict[str, object]


def str_to_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def float_or_none(value: Optional[str]) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_tank_events(env: Dict[str, str]) -> List[Event]:
    events: List[Event] = []
    sources = [
        ("brookside", env.get("BROOKSIDE_CSV")),
        ("roadside", env.get("ROADSIDE_CSV")),
    ]
    for tank_id, path_value in sources:
        if not path_value:
            continue
        path = repo_path_from_config(path_value)
        if not path.exists():
            LOGGER.warning("Tank CSV %s not found, skipping %s", path, tank_id)
            continue
        with path.open(newline="") as fp:
            reader = csv.DictReader(fp)
            for row in reader:
                ts_raw = row.get("timestamp") or row.get("Time")
                if not ts_raw:
                    continue
                timestamp = parse_timestamp(ts_raw)
                events.append(
                    Event(
                        timestamp=timestamp,
                        kind="tank",
                        data={
                            "tank_id": tank_id,
                            "source_timestamp": timestamp.isoformat(),
                            "volume_gal": float_or_none(row.get("gal")),
                            "depth": float_or_none(row.get("depth")),
                            "surf_dist": float_or_none(row.get("surf_dist")),
                        },
                    )
                )
    return events


def load_pump_events(env: Dict[str, str]) -> List[Event]:
    events: List[Event] = []
    csv_path_value = env.get("PUMP_EVENTS_CSV")
    if not csv_path_value:
        return events
    path = repo_path_from_config(csv_path_value)
    if not path.exists():
        LOGGER.warning("Pump CSV %s not found, skipping pump replay", path)
        return events
    with path.open(newline="") as fp:
        reader = csv.DictReader(fp)
        for row in reader:
            ts_raw = row.get("Time") or row.get("timestamp")
            if not ts_raw:
                continue
            timestamp = parse_timestamp(ts_raw)
            events.append(
                Event(
                    timestamp=timestamp,
                    kind="pump",
                    data={
                        "event_type": row.get("Pump Event") or row.get("event_type"),
                        "source_timestamp": timestamp.isoformat(),
                        "pump_run_time_s": float_or_none(
                            row.get("Pump Run Time") or row.get("pump_run_time_s")
                        ),
                        "pump_interval_s": float_or_none(
                            row.get("Pump Interval") or row.get("pump_interval_s")
                        ),
                        "gallons_per_hour": float_or_none(
                            row.get("Gallons Per Hour") or row.get("gallons_per_hour")
                        ),
                    },
                )
            )
    return events


def calc_percent(volume: Optional[float], capacity: Optional[float]) -> Optional[float]:
    if volume is None or not capacity:
        return None
    return max(0.0, min(100.0, (volume / capacity) * 100.0))


def atomic_write(path: Path, payload: Dict[str, object]) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path.write_text(json.dumps(payload, indent=2))
    tmp_path.replace(path)


def load_capacities(env: Dict[str, str]) -> Dict[str, Optional[float]]:
    caps = {}
    for tank_id in ("brookside", "roadside"):
        key = f"TANK_CAPACITY_{tank_id.upper()}"
        caps[tank_id] = float_or_none(env.get(key))
    return caps


def run_debug_loop(env: Dict[str, str]) -> None:
    include_tanks = str_to_bool(env.get("DEBUG_TANK"), True)
    include_pump = str_to_bool(env.get("DEBUG_RELEASER"), False)

    events: List[Event] = []
    if include_tanks:
        events.extend(load_tank_events(env))
    if include_pump:
        events.extend(load_pump_events(env))

    if not events:
        LOGGER.error("No debug events found. Check CSV paths in tank_pi.env.")
        return

    events.sort(key=lambda ev: ev.timestamp)
    start_timestamp = events[0].timestamp
    multiplier = float(env.get("SYNTHETIC_CLOCK_MULTIPLIER", "4.0"))
    loop_forever = str_to_bool(env.get("DEBUG_LOOP_DATA"), True)

    status_path = repo_path_from_config(env["STATUS_JSON_PATH"])
    caps = load_capacities(env)

    while True:
        LOGGER.info("Starting debug replay at %s (x%s)", start_timestamp, multiplier)
        clock = SyntheticClock(start_timestamp, multiplier)
        emit_events(events, clock, status_path, caps)
        if not loop_forever:
            break
        LOGGER.info("Replay complete; restarting in 10 seconds.")
        time.sleep(10)


def emit_events(
    events: Iterable[Event],
    clock: SyntheticClock,
    status_path: Path,
    capacities: Dict[str, Optional[float]],
) -> None:
    tanks: Dict[str, Dict[str, object]] = {}
    pump_info: Optional[Dict[str, object]] = None

    for event in events:
        clock.wait_until(event.timestamp)
        now_iso = clock.now().isoformat()

        if event.kind == "tank":
            tank_id = event.data["tank_id"]  # type: ignore[index]
            payload = {
                "tank_id": tank_id,
                "volume_gal": event.data.get("volume_gal"),
                "capacity_gal": capacities.get(tank_id),
                "level_percent": calc_percent(
                    event.data.get("volume_gal"), capacities.get(tank_id)
                ),
                "flow_gph": event.data.get("flow_gph"),
                "eta_full": None,
                "eta_empty": None,
                "last_sample_timestamp": event.data["source_timestamp"],
                "last_received_at": now_iso,
            }
            tanks[tank_id] = payload
        elif event.kind == "pump":
            pump_info = {
                "event_type": event.data.get("event_type"),
                "pump_run_time_s": event.data.get("pump_run_time_s"),
                "pump_interval_s": event.data.get("pump_interval_s"),
                "gallons_per_hour": event.data.get("gallons_per_hour"),
                "last_event_timestamp": event.data["source_timestamp"],
                "last_received_at": now_iso,
            }

        atomic_write(
            status_path,
            {
                "generated_at": now_iso,
                "tanks": tanks,
                "pump": pump_info,
            },
        )


def start_static_server(web_root: Path, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(web_root), **kwargs)

    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    LOGGER.info("Serving %s at http://%s:%s/", web_root, host, port)
    return server


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    try:
        env = load_role("tank_pi", required=["STATUS_JSON_PATH"])
    except Exception as exc:  # pragma: no cover - startup guard
        LOGGER.error("Failed to load tank_pi env: %s", exc)
        sys.exit(1)

    host = env.get("LOCAL_HTTP_HOST", "0.0.0.0")
    port = int(env.get("LOCAL_HTTP_PORT", "8080"))
    web_root = repo_path_from_config(env.get("WEB_ROOT", "web"))
    start_static_server(web_root, host, port)

    def handle_signal(signum, frame):
        LOGGER.info("Received signal %s, exiting.", signum)
        sys.exit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handle_signal)

    if str_to_bool(env.get("DEBUG_TANK"), False) or str_to_bool(
        env.get("DEBUG_RELEASER"), False
    ):
        run_debug_loop(env)
    else:
        LOGGER.info(
            "Hardware sampling not yet implemented in this refactor. "
            "Enable DEBUG_TANK=true to run CSV replay mode."
        )
        while True:
            time.sleep(60)


if __name__ == "__main__":
    main()
