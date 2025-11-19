#!/usr/bin/env python3
"""
Tank Pi orchestrator.

In debug mode we replay CSV files with the SyntheticClock, enqueue readings
exactly like live sensors would, persist them locally, upload through the
server API, and refresh the local web/data/status.json so both the WordPress
site and the Pi-hosted fallback UI can be exercised end-to-end.
"""
from __future__ import annotations

import csv
import json
import logging
import signal
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from urllib import error, request

from config_loader import load_role, repo_path_from_config
from synthetic_clock import SyntheticClock, parse_timestamp


LOGGER = logging.getLogger("tank_pi")


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def str_to_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def float_or_none(value) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_url(base: str, endpoint: str) -> str:
    return f"{base.rstrip('/')}/{endpoint.lstrip('/')}"


def post_json(url: str, payload: Dict | List, api_key: str, timeout: int = 10) -> Dict:
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": api_key,
    }
    req = request.Request(url, data=data, headers=headers, method="POST")
    with request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body or "{}")


class TankDatabase:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self._connect()

    def _connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.conn:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tank_readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tank_id TEXT NOT NULL,
                    source_timestamp TEXT NOT NULL,
                    surf_dist REAL,
                    depth REAL,
                    volume_gal REAL,
                    flow_gph REAL,
                    eta_full TEXT,
                    eta_empty TEXT,
                    time_to_full_min REAL,
                    time_to_empty_min REAL,
                    received_at TEXT NOT NULL,
                    sent_to_server INTEGER DEFAULT 0,
                    acked_by_server INTEGER DEFAULT 0,
                    UNIQUE(tank_id, source_timestamp)
                )
                """
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pump_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_type TEXT NOT NULL,
                    source_timestamp TEXT NOT NULL,
                    pump_run_time_s REAL,
                    pump_interval_s REAL,
                    gallons_per_hour REAL,
                    received_at TEXT NOT NULL,
                    sent_to_server INTEGER DEFAULT 0,
                    acked_by_server INTEGER DEFAULT 0,
                    UNIQUE(event_type, source_timestamp)
                )
                """
            )

    def reset(self) -> None:
        with self.lock:
            self.conn.close()
            if self.path.exists():
                self.path.unlink()
            self._connect()

    def insert_tank_reading(
        self, record: Dict[str, object], received_at: Optional[str] = None
    ) -> None:
        payload = {
            "tank_id": record["tank_id"],
            "source_timestamp": record["source_timestamp"],
            "surf_dist": float_or_none(record.get("surf_dist")),
            "depth": float_or_none(record.get("depth")),
            "volume_gal": float_or_none(record.get("volume_gal")),
            "flow_gph": float_or_none(record.get("flow_gph")),
            "eta_full": record.get("eta_full"),
            "eta_empty": record.get("eta_empty"),
            "time_to_full_min": float_or_none(record.get("time_to_full_min")),
            "time_to_empty_min": float_or_none(record.get("time_to_empty_min")),
            "received_at": received_at or iso_now(),
        }
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO tank_readings (
                    tank_id, source_timestamp, surf_dist, depth, volume_gal, flow_gph,
                    eta_full, eta_empty, time_to_full_min, time_to_empty_min, received_at
                ) VALUES (
                    :tank_id, :source_timestamp, :surf_dist, :depth, :volume_gal,
                    :flow_gph, :eta_full, :eta_empty, :time_to_full_min,
                    :time_to_empty_min, :received_at
                )
                """,
                payload,
            )

    def insert_pump_event(
        self, record: Dict[str, object], received_at: Optional[str] = None
    ) -> None:
        payload = {
            "event_type": record["event_type"],
            "source_timestamp": record["source_timestamp"],
            "pump_run_time_s": float_or_none(record.get("pump_run_time_s")),
            "pump_interval_s": float_or_none(record.get("pump_interval_s")),
            "gallons_per_hour": float_or_none(record.get("gallons_per_hour")),
            "received_at": received_at or iso_now(),
        }
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO pump_events (
                    event_type, source_timestamp, pump_run_time_s,
                    pump_interval_s, gallons_per_hour, received_at
                ) VALUES (
                    :event_type, :source_timestamp, :pump_run_time_s,
                    :pump_interval_s, :gallons_per_hour, :received_at
                )
                """,
                payload,
            )

    def latest_tank_rows(self) -> Dict[str, sqlite3.Row]:
        with self.lock:
            cur = self.conn.execute(
                """
                SELECT tr.*
                FROM tank_readings tr
                INNER JOIN (
                    SELECT tank_id, MAX(received_at) AS max_received
                    FROM tank_readings
                    GROUP BY tank_id
                ) latest ON latest.tank_id = tr.tank_id
                    AND latest.max_received = tr.received_at
                """
            )
            return {row["tank_id"]: row for row in cur.fetchall()}

    def latest_pump_row(self) -> Optional[sqlite3.Row]:
        with self.lock:
            cur = self.conn.execute(
                """
                SELECT *
                FROM pump_events
                ORDER BY received_at DESC
                LIMIT 1
                """
            )
            return cur.fetchone()

    def fetch_unsent_tank(self, limit: int) -> List[sqlite3.Row]:
        with self.lock:
            cur = self.conn.execute(
                """
                SELECT *
                FROM tank_readings
                WHERE acked_by_server = 0
                ORDER BY source_timestamp
                LIMIT ?
                """,
                (limit,),
            )
            return cur.fetchall()

    def mark_tank_acked(self, ids: List[int]) -> None:
        if not ids:
            return
        with self.lock, self.conn:
            self.conn.executemany(
                """
                UPDATE tank_readings
                SET sent_to_server = 1, acked_by_server = 1
                WHERE id = ?
                """,
                [(row_id,) for row_id in ids],
            )

    def fetch_unsent_pump(self, limit: int) -> List[sqlite3.Row]:
        with self.lock:
            cur = self.conn.execute(
                """
                SELECT *
                FROM pump_events
                WHERE acked_by_server = 0
                ORDER BY source_timestamp
                LIMIT ?
                """,
                (limit,),
            )
            return cur.fetchall()

    def mark_pump_acked(self, ids: List[int]) -> None:
        if not ids:
            return
        with self.lock, self.conn:
            self.conn.executemany(
                """
                UPDATE pump_events
                SET sent_to_server = 1, acked_by_server = 1
                WHERE id = ?
                """,
                [(row_id,) for row_id in ids],
            )


class StatusWriter:
    def __init__(
        self, db: TankDatabase, status_path: Path, capacities: Dict[str, Optional[float]]
    ):
        self.db = db
        self.status_path = status_path
        self.capacities = capacities

    def write(self) -> None:
        tanks = {}
        for tank_id, row in self.db.latest_tank_rows().items():
            cap = self.capacities.get(tank_id)
            volume = row["volume_gal"]
            tanks[tank_id] = {
                "tank_id": tank_id,
                "volume_gal": volume,
                "capacity_gal": cap,
                "level_percent": self.calc_percent(volume, cap),
                "flow_gph": row["flow_gph"],
                "eta_full": row["eta_full"],
                "eta_empty": row["eta_empty"],
                "last_sample_timestamp": row["source_timestamp"],
                "last_received_at": row["received_at"],
            }

        pump_row = self.db.latest_pump_row()
        pump_section = None
        if pump_row:
            pump_section = {
                "event_type": pump_row["event_type"],
                "pump_run_time_s": pump_row["pump_run_time_s"],
                "pump_interval_s": pump_row["pump_interval_s"],
                "gallons_per_hour": pump_row["gallons_per_hour"],
                "last_event_timestamp": pump_row["source_timestamp"],
                "last_received_at": pump_row["received_at"],
            }

        payload = {
            "generated_at": iso_now(),
            "tanks": tanks,
            "pump": pump_section,
        }
        self._atomic_write(payload)

    def write_empty(self) -> None:
        self._atomic_write({"generated_at": None, "tanks": {}, "pump": None})

    def _atomic_write(self, payload: Dict) -> None:
        tmp_path = self.status_path.with_suffix(self.status_path.suffix + ".tmp")
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(json.dumps(payload, indent=2))
        tmp_path.replace(self.status_path)

    @staticmethod
    def calc_percent(volume: Optional[float], capacity: Optional[float]) -> Optional[float]:
        if volume is None or capacity in (None, 0):
            return None
        return max(0.0, min(100.0, (float(volume) / float(capacity)) * 100.0))


class UploadWorker:
    def __init__(self, env: Dict[str, str], db: TankDatabase):
        self.db = db
        self.api_base = env["API_BASE_URL"]
        self.api_key = env["API_KEY"]
        self.tank_batch = int(env.get("UPLOAD_BATCH_SIZE", "4"))
        self.tank_interval = int(env.get("UPLOAD_INTERVAL_SECONDS", "60"))
        self.pump_batch = int(env.get("PUMP_UPLOAD_BATCH_SIZE", "1"))
        self.pump_interval = int(env.get("PUMP_UPLOAD_INTERVAL_SECONDS", "60"))
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.stop_event = threading.Event()

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=2)

    def _run(self) -> None:
        next_tank = time.monotonic()
        next_pump = time.monotonic()
        while not self.stop_event.wait(1):
            now = time.monotonic()
            if now >= next_tank:
                self._upload_tank()
                next_tank = now + self.tank_interval
            if now >= next_pump:
                self._upload_pump()
                next_pump = now + self.pump_interval

    def _upload_tank(self) -> None:
        rows = self.db.fetch_unsent_tank(self.tank_batch)
        if not rows:
            return
        payload = [
            {
                "tank_id": row["tank_id"],
                "source_timestamp": row["source_timestamp"],
                "surf_dist": row["surf_dist"],
                "depth": row["depth"],
                "volume_gal": row["volume_gal"],
                "flow_gph": row["flow_gph"],
                "eta_full": row["eta_full"],
                "eta_empty": row["eta_empty"],
                "time_to_full_min": row["time_to_full_min"],
                "time_to_empty_min": row["time_to_empty_min"],
            }
            for row in rows
        ]
        try:
            url = build_url(self.api_base, "ingest_tank.php")
            resp = post_json(url, payload, self.api_key)
            LOGGER.info("Uploaded %s tank readings (resp=%s)", len(rows), resp.get("status"))
            self.db.mark_tank_acked([row["id"] for row in rows])
        except error.URLError as exc:
            LOGGER.warning("Tank upload failed: %s", exc)

    def _upload_pump(self) -> None:
        rows = self.db.fetch_unsent_pump(self.pump_batch)
        if not rows:
            return
        payload = [
            {
                "event_type": row["event_type"],
                "source_timestamp": row["source_timestamp"],
                "pump_run_time_s": row["pump_run_time_s"],
                "pump_interval_s": row["pump_interval_s"],
                "gallons_per_hour": row["gallons_per_hour"],
            }
            for row in rows
        ]
        try:
            url = build_url(self.api_base, "ingest_pump.php")
            resp = post_json(url, payload, self.api_key)
            LOGGER.info("Uploaded %s pump events (resp=%s)", len(rows), resp.get("status"))
            self.db.mark_pump_acked([row["id"] for row in rows])
        except error.URLError as exc:
            LOGGER.warning("Pump upload failed: %s", exc)


@dataclass
class Event:
    timestamp: datetime
    kind: str  # "tank" or "pump"
    data: Dict[str, object]


def load_tank_events(env: Dict[str, str]) -> List[Event]:
    events: List[Event] = []
    for tank_id, key in (("brookside", "BROOKSIDE_CSV"), ("roadside", "ROADSIDE_CSV")):
        path_value = env.get(key)
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
                            "surf_dist": row.get("surf_dist"),
                            "depth": row.get("depth"),
                            "volume_gal": row.get("gal") or row.get("volume_gal"),
                            "flow_gph": row.get("flow_gph"),
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
                        "pump_run_time_s": row.get("Pump Run Time")
                        or row.get("pump_run_time_s"),
                        "pump_interval_s": row.get("Pump Interval")
                        or row.get("pump_interval_s"),
                        "gallons_per_hour": row.get("Gallons Per Hour")
                        or row.get("gallons_per_hour"),
                    },
                )
            )
    return events


def start_static_server(web_root: Path, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(SimpleHTTPRequestHandler):
        prefix = "/sugar_house_monitor"

        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(web_root), **kwargs)

        def translate_path(self, path: str) -> str:
            if path.startswith(self.prefix):
                stripped = path[len(self.prefix) :]
                path = stripped if stripped.startswith("/") else f"/{stripped}"
                if path == "/":
                    path = "/index.html"
            return super().translate_path(path)

    server = ThreadingHTTPServer((host, port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    LOGGER.info("Serving %s at http://%s:%s/", web_root, host, port)
    return server


class TankPiApp:
    def __init__(self, env: Dict[str, str]):
        self.env = env
        self.db = TankDatabase(repo_path_from_config(env.get("DB_PATH", "data/tank_pi.db")))
        capacities = {
            "brookside": float_or_none(env.get("TANK_CAPACITY_BROOKSIDE")),
            "roadside": float_or_none(env.get("TANK_CAPACITY_ROADSIDE")),
        }
        self.status_writer = StatusWriter(
            self.db, repo_path_from_config(env["STATUS_JSON_PATH"]), capacities
        )
        self.upload_worker = UploadWorker(env, self.db)
        self.debug_enabled = str_to_bool(env.get("DEBUG_TANK"), False) or str_to_bool(
            env.get("DEBUG_RELEASER"), False
        )

    def reset_if_needed(self) -> None:
        if not self.debug_enabled:
            return
        if not str_to_bool(self.env.get("RESET_ON_DEBUG_START"), True):
            return
        LOGGER.info("Resetting local DB/state for debug replay")
        self.db.reset()
        self.status_writer.write_empty()
        self.reset_server_state()

    def reset_server_state(self) -> None:
        try:
            payload = {"action": "reset_all"}
            url = build_url(self.env["API_BASE_URL"], "reset.php")
            resp = post_json(url, payload, self.env["API_KEY"])
            LOGGER.info("Server reset response: %s", resp.get("status"))
        except error.URLError as exc:
            LOGGER.warning("Unable to reset server state: %s", exc)

    def handle_tank_measurement(
        self, payload: Dict[str, object], received_at: Optional[str] = None
    ) -> None:
        self.db.insert_tank_reading(payload, received_at)
        self.status_writer.write()
        LOGGER.info(
            "Synthetic tank sample %s @ %s volume=%s gal",
            payload["tank_id"],
            payload["source_timestamp"],
            payload.get("volume_gal"),
        )

    def handle_pump_event(
        self, payload: Dict[str, object], received_at: Optional[str] = None
    ) -> None:
        if not payload.get("event_type"):
            return
        self.db.insert_pump_event(payload, received_at)
        self.status_writer.write()
        LOGGER.info(
            "Synthetic pump event %s @ %s run=%s s interval=%s s",
            payload.get("event_type"),
            payload.get("source_timestamp"),
            payload.get("pump_run_time_s"),
            payload.get("pump_interval_s"),
        )

    def run_debug_loop(self) -> None:
        events: List[Event] = []
        if str_to_bool(self.env.get("DEBUG_TANK"), False):
            events.extend(load_tank_events(self.env))
        if str_to_bool(self.env.get("DEBUG_RELEASER"), False):
            events.extend(load_pump_events(self.env))
        if not events:
            LOGGER.error("No debug events found. Check CSV paths in tank_pi.env.")
            return
        events.sort(key=lambda ev: ev.timestamp)
        start_timestamp = events[0].timestamp
        multiplier = float(self.env.get("SYNTHETIC_CLOCK_MULTIPLIER", "4.0"))
        loop_forever = str_to_bool(self.env.get("DEBUG_LOOP_DATA"), True)

        while True:
            LOGGER.info("Starting debug replay at %s (x%s)", start_timestamp, multiplier)
            clock = SyntheticClock(start_timestamp, multiplier)
            for event in events:
                clock.wait_until(event.timestamp)
                now_iso = clock.now().isoformat()
                if event.kind == "tank":
                    self.handle_tank_measurement(event.data, now_iso)
                else:
                    self.handle_pump_event(event.data, now_iso)
            if not loop_forever:
                break
            LOGGER.info("Replay complete; restarting in 10 seconds.")
            time.sleep(10)

    def run(self) -> None:
        self.reset_if_needed()
        self.status_writer.write()
        self.upload_worker.start()

        host = self.env.get("LOCAL_HTTP_HOST", "0.0.0.0")
        port = int(self.env.get("LOCAL_HTTP_PORT", "8080"))
        web_root = repo_path_from_config(self.env.get("WEB_ROOT", "web"))
        start_static_server(web_root, host, port)

        if self.debug_enabled:
            self.run_debug_loop()
        else:
            LOGGER.info(
                "Hardware sampling not yet implemented in this refactor. "
                "Enable DEBUG_TANK=true to run CSV replay mode."
            )
            while True:
                time.sleep(60)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        env = load_role("tank_pi", required=["STATUS_JSON_PATH", "API_BASE_URL", "API_KEY"])
    except Exception as exc:
        LOGGER.error("Failed to load tank_pi env: %s", exc)
        sys.exit(1)

    app = TankPiApp(env)

    def handle_signal(sig, frame):
        LOGGER.info("Received signal %s, shutting down.", sig)
        app.upload_worker.stop()
        sys.exit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handle_signal)

    app.run()


if __name__ == "__main__":
    main()
