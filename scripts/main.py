#!/usr/bin/env python3
"""
Tank Pi orchestrator.

In debug mode we replay CSV files with the SyntheticClock, enqueue readings
exactly like live sensors would, persist them locally, upload through the
server API, and refresh the local web/data/status_*.json files so both the WordPress
site and the Pi-hosted fallback UI can be exercised end-to-end.
"""
from __future__ import annotations

import csv
import math
import json
import logging
import queue
import random
import signal
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from multiprocessing import Process, current_process
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from urllib import error, parse, request

from config_loader import load_role, repo_path_from_config
import tank_vol_fcns as TVF
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


def _append_api_key(url: str, api_key: str) -> str:
    parsed = parse.urlparse(url)
    query = parse.parse_qsl(parsed.query, keep_blank_values=True)
    if not any(key.lower() == "api_key" for key, _ in query):
        query.append(("api_key", api_key))
    new_query = parse.urlencode(query)
    return parse.urlunparse(parsed._replace(query=new_query))


def post_json(url: str, payload: Dict | List, api_key: str, timeout: int = 10) -> Dict:
    url = _append_api_key(url, api_key)
    if isinstance(payload, dict):
        payload = dict(payload)
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": api_key,
    }
    req = request.Request(url, data=data, headers=headers, method="POST")
    with request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
        return json.loads(body or "{}")


def log_http_error(prefix: str, exc: error.URLError) -> None:
    if isinstance(exc, error.HTTPError):
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = "<unavailable>"
        LOGGER.warning("%s: %s (response=%s)", prefix, exc, body)
    else:
        LOGGER.warning("%s: %s", prefix, exc)


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
                    max_volume_gal REAL,
                    level_percent REAL,
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
            self._ensure_column("tank_readings", "max_volume_gal", "REAL")
            self._ensure_column("tank_readings", "level_percent", "REAL")
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
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS vacuum_readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reading_inhg REAL,
                    source_timestamp TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    sent_to_server INTEGER DEFAULT 0,
                    acked_by_server INTEGER DEFAULT 0,
                    UNIQUE(source_timestamp)
                )
                """
            )

    def reset(self) -> None:
        with self.lock:
            self.conn.close()
            if self.path.exists():
                self.path.unlink()
            self._connect()

    def _ensure_column(self, table: str, column: str, definition: str) -> None:
        cur = self.conn.execute(f"PRAGMA table_info({table})")
        columns = {row["name"] for row in cur.fetchall()}
        if column not in columns:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def insert_tank_reading(
        self, record: Dict[str, object], received_at: Optional[str] = None
    ) -> None:
        payload = {
            "tank_id": record["tank_id"],
            "source_timestamp": record["source_timestamp"],
            "surf_dist": float_or_none(record.get("surf_dist")),
            "depth": float_or_none(record.get("depth")),
            "volume_gal": float_or_none(record.get("volume_gal")),
             "max_volume_gal": float_or_none(record.get("max_volume_gal")),
             "level_percent": float_or_none(record.get("level_percent")),
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
                    tank_id, source_timestamp, surf_dist, depth, volume_gal, max_volume_gal,
                    level_percent, flow_gph, eta_full, eta_empty, time_to_full_min,
                    time_to_empty_min, received_at
                ) VALUES (
                    :tank_id, :source_timestamp, :surf_dist, :depth, :volume_gal,
                    :max_volume_gal, :level_percent, :flow_gph, :eta_full, :eta_empty,
                    :time_to_full_min, :time_to_empty_min, :received_at
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

    def insert_vacuum_reading(
        self, record: Dict[str, object], received_at: Optional[str] = None
    ) -> None:
        payload = {
            "reading_inhg": float_or_none(record.get("reading_inhg")),
            "source_timestamp": record["source_timestamp"],
            "received_at": received_at or iso_now(),
        }
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO vacuum_readings (
                    reading_inhg, source_timestamp, received_at
                ) VALUES (
                    :reading_inhg, :source_timestamp, :received_at
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

    def fetch_unsent_vacuum(self, limit: int) -> List[sqlite3.Row]:
        with self.lock:
            cur = self.conn.execute(
                """
                SELECT *
                FROM vacuum_readings
                WHERE acked_by_server = 0
                ORDER BY source_timestamp
                LIMIT ?
                """,
                (limit,),
            )
            return cur.fetchall()

    def mark_vacuum_acked(self, ids: List[int]) -> None:
        if not ids:
            return
        with self.lock, self.conn:
            self.conn.executemany(
                """
                UPDATE vacuum_readings
                SET sent_to_server = 1, acked_by_server = 1
                WHERE id = ?
                """,
                [(row_id,) for row_id in ids],
            )


class UploadWorker:
    def __init__(self, env: Dict[str, str], db: TankDatabase, speed_factor: float = 1.0):
        self.db = db
        self.api_base = env["API_BASE_URL"]
        self.api_key = env["API_KEY"]
        base_tank_batch = int(env.get("UPLOAD_BATCH_SIZE", "4"))
        base_tank_interval = int(env.get("UPLOAD_INTERVAL_SECONDS", "60"))
        base_pump_batch = int(env.get("PUMP_UPLOAD_BATCH_SIZE", "1"))
        base_pump_interval = int(env.get("PUMP_UPLOAD_INTERVAL_SECONDS", "60"))
        self.handshake_interval = float(env.get("HANDSHAKE_INTERVAL_SECONDS", "60"))
        self.tank_batch = base_tank_batch
        self.tank_interval = base_tank_interval
        self.pump_batch = base_pump_batch
        self.pump_interval = base_pump_interval
        self.vacuum_batch = int(env.get("VACUUM_UPLOAD_BATCH_SIZE", "8"))
        self.vacuum_interval = int(env.get("VACUUM_UPLOAD_INTERVAL_SECONDS", "30"))
        self.speed_factor = max(speed_factor, 1.0)
        if self.speed_factor > 1.0:
            # Speed up uploads when the synthetic clock is running faster than real time.
            self.tank_interval = max(1, int(base_tank_interval / self.speed_factor))
            self.pump_interval = max(1, int(base_pump_interval / self.speed_factor))
            # Increase batch sizes so accelerated debug runs can keep up with simulated data.
            self.tank_batch = max(1, int(math.ceil(base_tank_batch * self.speed_factor)))
            self.pump_batch = max(1, int(math.ceil(base_pump_batch * self.speed_factor)))
            self.vacuum_batch = max(1, int(math.ceil(self.vacuum_batch * self.speed_factor)))
            self.vacuum_interval = max(1, int(self.vacuum_interval / self.speed_factor))
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.stop_event = threading.Event()
        now = time.monotonic()
        self._last_tank_handshake = now
        self._last_pump_handshake = now

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=2)

    def _run(self) -> None:
        next_tank = time.monotonic()
        next_pump = time.monotonic()
        next_vac = time.monotonic()
        while not self.stop_event.wait(1):
            now = time.monotonic()
            if now >= next_tank:
                sent = self._upload_tank()
                if sent:
                    self._last_tank_handshake = now
                elif now - self._last_tank_handshake >= self.handshake_interval:
                    self._send_handshake("tank")
                    self._last_tank_handshake = now
                next_tank = now + self.tank_interval
            if now >= next_pump:
                sent = self._upload_pump()
                if sent:
                    self._last_pump_handshake = now
                elif now - self._last_pump_handshake >= self.handshake_interval:
                    self._send_handshake("pump")
                    self._last_pump_handshake = now
                next_pump = now + self.pump_interval
            if now >= next_vac:
                self._upload_vacuum()
                next_vac = now + self.vacuum_interval

    def _upload_tank(self) -> bool:
        rows = self.db.fetch_unsent_tank(self.tank_batch)
        if not rows:
            return False
        readings = [
            {
                "tank_id": row["tank_id"],
                "source_timestamp": row["source_timestamp"],
                "surf_dist": row["surf_dist"],
                "depth": row["depth"],
                "volume_gal": row["volume_gal"],
                "max_volume_gal": row["max_volume_gal"],
                "level_percent": row["level_percent"],
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
            resp = post_json(url, {"readings": readings}, self.api_key)
            LOGGER.info("Uploaded %s tank readings (resp=%s)", len(rows), resp.get("status"))
            self.db.mark_tank_acked([row["id"] for row in rows])
        except error.URLError as exc:
            log_http_error("Tank upload failed", exc)
            return False
        return True

    def _upload_pump(self) -> bool:
        rows = self.db.fetch_unsent_pump(self.pump_batch)
        if not rows:
            return False
        events = [
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
            resp = post_json(url, {"events": events}, self.api_key)
            LOGGER.info("Uploaded %s pump events (resp=%s)", len(rows), resp.get("status"))
            self.db.mark_pump_acked([row["id"] for row in rows])
        except error.URLError as exc:
            log_http_error("Pump upload failed", exc)
            return False
        return True

    def _send_handshake(self, stream: str) -> None:
        try:
            url = build_url(self.api_base, "ingest_nodata.php")
            payload = {"stream": stream}
            resp = post_json(url, payload, self.api_key)
            LOGGER.info("Sent %s handshake (resp=%s)", stream, resp.get("status"))
        except error.URLError as exc:
            log_http_error(f"{stream.capitalize()} handshake failed", exc)

    def _upload_vacuum(self) -> bool:
        rows = self.db.fetch_unsent_vacuum(self.vacuum_batch)
        if not rows:
            return False
        payload = [
            {
                "reading_inhg": row["reading_inhg"],
                "source_timestamp": row["source_timestamp"],
            }
            for row in rows
        ]
        try:
            url = build_url(self.api_base, "ingest_vacuum.php")
            resp = post_json(url, {"readings": payload}, self.api_key)
            LOGGER.info("Uploaded %s vacuum readings (resp=%s)", len(rows), resp.get("status"))
            self.db.mark_vacuum_acked([row["id"] for row in rows])
        except error.URLError as exc:
            log_http_error("Vacuum upload failed", exc)
            return False
        return True


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
    events.sort(key=lambda ev: ev.timestamp)
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
    events.sort(key=lambda ev: ev.timestamp)
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
        self.debug_enabled = str_to_bool(env.get("DEBUG_TANK"), False) or str_to_bool(
            env.get("DEBUG_RELEASER"), False
        )
        try:
            self.clock_multiplier = float(self.env.get("SYNTHETIC_CLOCK_MULTIPLIER", "1") or "1")
        except ValueError:
            self.clock_multiplier = 1.0
        speed_factor = self.clock_multiplier if self.debug_enabled else 1.0
        self.upload_worker = UploadWorker(env, self.db, speed_factor=speed_factor)
        self.loop_debug_data = str_to_bool(env.get("DEBUG_LOOP_DATA"), False)
        self.debug_loop_gap = timedelta(
            seconds=float(self.env.get("DEBUG_LOOP_GAP_SECONDS", "10"))
        )
        status_path = repo_path_from_config(env["STATUS_JSON_PATH"])
        self.status_dir = status_path.parent
        self.status_dir.mkdir(parents=True, exist_ok=True)
        self.pump_status_path = self.status_dir / "status_pump.json"
        self.vacuum_status_path = self.status_dir / "status_vacuum.json"
        self.measurement_params = self._build_measurement_params()
        self.debug_clock: Optional[SyntheticClock] = None
        self.debug_records: Dict[str, List[TVF.DebugSample]] = {}
        self.tank_processes: Dict[str, Process] = {}
        self.lcd_process: Optional[Process] = None
        self.collector_thread: Optional[threading.Thread] = None
        self.pump_thread: Optional[threading.Thread] = None
        self.vacuum_thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()
        self._ensure_status_placeholders()

    def reset_if_needed(self) -> None:
        if not self.debug_enabled:
            return
        if not str_to_bool(self.env.get("RESET_ON_DEBUG_START"), True):
            return
        LOGGER.info("Resetting local DB/state for debug replay")
        self.db.reset()
        self._clear_status_files()
        self._ensure_status_placeholders()
        self.reset_server_state()

    def _build_measurement_params(self):
        return (
            int(self.env.get("TANK_NUM_TO_AVERAGE", "8")),
            float(self.env.get("TANK_MEAS_DELAY", "0.25")),
            float(self.env.get("TANK_READINGS_PER_MIN", "4")),
            int(self.env.get("TANK_FILTER_WINDOW", "50")),
            float(self.env.get("TANK_FILTER_SIGMA", "0.25")),
            int(self.env.get("TANK_RATE_UPDATE_SECONDS", "15")),
        )

    def _prepare_debug_inputs(self):
        tank_records: Dict[str, List[TVF.DebugSample]] = {}
        pump_events: List[Event] = []
        debug_tank = str_to_bool(self.env.get("DEBUG_TANK"), False)
        debug_pump = str_to_bool(self.env.get("DEBUG_RELEASER"), False)
        tank_events = load_tank_events(self.env) if debug_tank else []
        if debug_pump:
            pump_events = load_pump_events(self.env)
        if debug_tank and not tank_events:
            LOGGER.error("DEBUG_TANK enabled but no tank CSV rows were found.")
        if debug_pump and not pump_events:
            LOGGER.error("DEBUG_RELEASER enabled but no pump CSV rows were found.")
        if not tank_events and not pump_events:
            return None, tank_records, pump_events
        start_timestamp = min(
            event.timestamp for event in (tank_events + pump_events) if event.timestamp
        )
        multiplier = float(self.env.get("SYNTHETIC_CLOCK_MULTIPLIER", "4.0"))
        clock = SyntheticClock(start_timestamp, multiplier)

        for name in TVF.tank_names:
            tank_records[name] = []
        for event in tank_events:
            tank_id = event.data.get("tank_id")
            if not tank_id:
                continue
            surf = float_or_none(event.data.get("surf_dist"))
            if surf is None:
                continue
            sample = TVF.DebugSample(
                timestamp=event.timestamp,
                surf_dist=surf,
                depth=float_or_none(event.data.get("depth")),
                volume_gal=float_or_none(event.data.get("volume_gal")),
                flow_gph=float_or_none(event.data.get("flow_gph")),
            )
            tank_records.setdefault(tank_id, []).append(sample)

        return clock, tank_records, pump_events

    def _clear_status_files(self) -> None:
        for path in self.status_dir.glob("status_*.json"):
            try:
                path.unlink()
            except FileNotFoundError:
                continue

    def _ensure_status_placeholders(self) -> None:
        """Create empty status files so local HTTP requests do not 404 before data arrives."""
        timestamp = iso_now()
        for tank_name in TVF.tank_names:
            path = self.status_dir / f"status_{tank_name}.json"
            if not path.exists():
                placeholder = {
                    "generated_at": timestamp,
                    "tank_id": tank_name,
                    "volume_gal": None,
                    "max_volume_gal": None,
                    "level_percent": None,
                    "flow_gph": None,
                    "eta_full": None,
                    "eta_empty": None,
                    "time_to_full_min": None,
                    "time_to_empty_min": None,
                    "last_sample_timestamp": None,
                    "last_received_at": None,
                }
                self._write_status_file(path, placeholder)

        if self.pump_status_path and not self.pump_status_path.exists():
            placeholder = {
                "generated_at": timestamp,
                "event_type": None,
                "pump_run_time_s": None,
                "pump_interval_s": None,
                "gallons_per_hour": None,
                "last_event_timestamp": None,
                "last_received_at": None,
                "pump_status": "Unknown",
            }
            self._write_status_file(self.pump_status_path, placeholder)

        if self.vacuum_status_path and not self.vacuum_status_path.exists():
            vacuum_placeholder = {
                "generated_at": timestamp,
                "reading_inhg": None,
                "source_timestamp": None,
                "last_received_at": None,
            }
            self._write_status_file(self.vacuum_status_path, vacuum_placeholder)

    def _write_status_file(self, path: Path, payload: Dict) -> None:
        tmp = path.with_suffix(path.suffix + ".tmp")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(path)

    def _load_existing_json(self, path: Path) -> Dict:
        if not path or not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}

    def _start_tank_controller(
        self,
        tank_name: str,
        measurement_params,
        clock: Optional[SyntheticClock],
        tank_records: Optional[List[TVF.DebugSample]],
    ) -> Process:
        history_path = self.db.path
        debug_tank = str_to_bool(self.env.get("DEBUG_TANK"), False)
        records = tank_records if tank_records else None
        process_clock = clock if records else None
        if debug_tank and not records:
            LOGGER.warning(
                "No debug data found for tank %s; controller will wait for hardware.",
                tank_name,
            )
        proc = None
        for attempt in (1, 2):
            proc = Process(
                target=TVF.run_tank_controller,
                args=(tank_name, TVF.queue_dict, measurement_params),
                kwargs={
                    "clock": process_clock,
                    "debug_records": records,
                    "history_db_path": history_path,
                    "status_dir": self.status_dir,
                    "history_hours": getattr(TVF, "DEFAULT_HISTORY_HOURS", 6),
                    "loop_debug": self.loop_debug_data,
                    "loop_gap_seconds": self.debug_loop_gap.total_seconds(),
                },
                daemon=True,
            )
            proc.start()
            time.sleep(0.2)
            if proc.is_alive():
                break
            LOGGER.warning(
                "Tank %s controller failed to stay up (attempt %s)", tank_name, attempt
            )
        self.tank_processes[tank_name] = proc
        if proc.is_alive():
            LOGGER.info("Started %s tank controller (pid=%s)", tank_name, proc.pid)
        else:
            LOGGER.error("Failed to start tank controller %s after retries", tank_name)
        return proc

    def _start_tank_processes(
        self,
        measurement_params,
        clock: Optional[SyntheticClock],
        tank_records: Dict[str, List[TVF.DebugSample]],
    ) -> None:
        for tank_name in TVF.tank_names:
            self._start_tank_controller(
                tank_name,
                measurement_params,
                clock,
                tank_records.get(tank_name) if tank_records else None,
            )

    def _start_lcd_process(self) -> None:
        if self.lcd_process:
            return
        try:
            lcd = TVF.init_display()
        except Exception as exc:
            LOGGER.warning("LCD initialization failed: %s", exc)
            return
        proc = Process(target=TVF.run_lcd_screen, args=(lcd, TVF.queue_dict), daemon=True)
        proc.start()
        self.lcd_process = proc
        LOGGER.info("Started LCD process (pid=%s)", proc.pid)

    def _start_measurement_collector(self) -> None:
        if self.collector_thread and self.collector_thread.is_alive():
            return
        self.collector_thread = threading.Thread(target=self._drain_status_updates, daemon=True)
        self.collector_thread.start()

    def _drain_status_updates(self) -> None:
        queues = {name: TVF.queue_dict[name]["status_updates"] for name in TVF.tank_names}
        while not self.stop_event.is_set():
            processed = False
            for name, status_queue in queues.items():
                try:
                    payload = status_queue.get_nowait()
                except queue.Empty:
                    continue
                except EOFError:
                    continue
                if payload:
                    self.handle_tank_measurement(payload, payload.get("source_timestamp"))
                    processed = True
            if not processed:
                self.stop_event.wait(0.2)

    def _start_pump_debug_thread(
        self, clock: Optional[SyntheticClock], pump_events: List[Event]
    ) -> None:
        if not clock or not pump_events:
            return
        if self.pump_thread and self.pump_thread.is_alive():
            return
        self.pump_thread = threading.Thread(
            target=self._run_pump_debug, args=(clock, pump_events), daemon=True
        )
        self.pump_thread.start()

    def _start_vacuum_debug_thread(self, clock: Optional[SyntheticClock]) -> None:
        """Emit placeholder vacuum readings during debug runs."""
        if self.vacuum_thread and self.vacuum_thread.is_alive():
            return
        interval = max(1.0, 10.0 / max(self.clock_multiplier, 1.0))

        def _loop() -> None:
            while not self.stop_event.is_set():
                now = clock.now() if clock else datetime.now(timezone.utc)
                reading = round(random.uniform(-28.0, -5.0), 1)
                generated = iso_now()
                payload = {
                    "generated_at": generated,
                    "reading_inhg": reading,
                    "source_timestamp": now.isoformat(),
                    "last_received_at": generated,
                }
                self.db.insert_vacuum_reading(payload, payload["last_received_at"])
                if self.vacuum_status_path:
                    self._write_status_file(self.vacuum_status_path, payload)
                self.stop_event.wait(interval)

        self.vacuum_thread = threading.Thread(target=_loop, daemon=True)
        self.vacuum_thread.start()

    def _run_pump_debug(self, clock: SyntheticClock, events: List[Event]) -> None:
        if not events:
            return
        events = sorted(events, key=lambda ev: ev.timestamp)
        LOGGER.info(
            "Starting pump debug replay at %s (x%s)",
            events[0].timestamp,
            clock.multiplier,
        )
        if len(events) > 1:
            base_span = events[-1].timestamp - events[0].timestamp
        else:
            base_span = timedelta(seconds=0)
        cycle_offset = timedelta(0)
        while not self.stop_event.is_set():
            for event in events:
                if self.stop_event.is_set():
                    break
                scheduled = event.timestamp + cycle_offset
                clock.wait_until(scheduled)
                payload = dict(event.data)
                payload["source_timestamp"] = scheduled.isoformat()
                self.handle_pump_event(payload, clock.now().isoformat())
            if not self.loop_debug_data:
                break
            pause = base_span
            if pause <= timedelta(0):
                pause = self.debug_loop_gap
            cycle_offset += pause + self.debug_loop_gap
        LOGGER.info("Pump debug replay complete.")

    def _stop_tank_processes(self) -> None:
        for tank_name, proc in self.tank_processes.items():
            if not proc.is_alive():
                continue
            LOGGER.info("Stopping %s tank controller (pid=%s)", tank_name, proc.pid)
            proc.terminate()
            proc.join(timeout=2)
            if proc.is_alive():
                LOGGER.warning("%s tank controller still alive, killing (pid=%s)", tank_name, proc.pid)
                proc.kill()
                proc.join(timeout=2)
        self.tank_processes.clear()

    def _stop_lcd_process(self) -> None:
        if self.lcd_process and self.lcd_process.is_alive():
            LOGGER.info("Stopping LCD process (pid=%s)", self.lcd_process.pid)
            self.lcd_process.terminate()
            self.lcd_process.join(timeout=2)
            if self.lcd_process.is_alive():
                LOGGER.warning("LCD process still alive, killing (pid=%s)", self.lcd_process.pid)
                self.lcd_process.kill()
                self.lcd_process.join(timeout=2)
        self.lcd_process = None

    def _stop_measurement_collector(self) -> None:
        if self.collector_thread:
            self.collector_thread.join(timeout=2)
        self.collector_thread = None

    def _stop_pump_thread(self) -> None:
        if self.pump_thread:
            self.pump_thread.join(timeout=2)
        self.pump_thread = None

    def _stop_vacuum_thread(self) -> None:
        if self.vacuum_thread:
            self.vacuum_thread.join(timeout=2)
        self.vacuum_thread = None

    def _ensure_tank_processes_alive(self) -> None:
        for tank_name in TVF.tank_names:
            proc = self.tank_processes.get(tank_name)
            if proc and proc.is_alive():
                continue
            if proc:
                LOGGER.warning(
                    "Tank controller %s died (exitcode=%s); restarting.",
                    tank_name,
                    proc.exitcode,
                )
            self._start_tank_controller(
                tank_name,
                self.measurement_params,
                self.debug_clock,
                (self.debug_records or {}).get(tank_name),
            )

    def shutdown(self) -> None:
        if not self.stop_event.is_set():
            self.stop_event.set()
        else:
            LOGGER.info("Shutdown already in progress; forcing remaining processes to exit.")
        self.upload_worker.stop()
        self._stop_pump_thread()
        self._stop_vacuum_thread()
        self._stop_measurement_collector()
        self._stop_lcd_process()
        self._stop_tank_processes()

    def reset_server_state(self) -> None:
        try:
            payload = {"action": "reset_all"}
            url = build_url(self.env["API_BASE_URL"], "reset.php")
            resp = post_json(url, payload, self.env["API_KEY"])
            LOGGER.info("Server reset response: %s", resp.get("status"))
        except error.URLError as exc:
            log_http_error("Unable to reset server state", exc)

    def handle_tank_measurement(
        self, payload: Dict[str, object], received_at: Optional[str] = None
    ) -> None:
        self.db.insert_tank_reading(payload, received_at)
        LOGGER.info(
            "Tank sample %s @ %s volume=%s gal",
            payload["tank_id"],
            payload["source_timestamp"],
            payload.get("volume_gal"),
        )

    def handle_pump_event(
        self, payload: Dict[str, object], received_at: Optional[str] = None
    ) -> None:
        if not payload.get("event_type"):
            return
        previous_status = self._load_existing_json(self.pump_status_path) if self.pump_status_path else {}
        self.db.insert_pump_event(payload, received_at)
        if self.pump_status_path:
            run_time = float_or_none(payload.get("pump_run_time_s"))
            interval = float_or_none(payload.get("pump_interval_s"))
            gph = float_or_none(payload.get("gallons_per_hour"))
            if gph is None:
                gph = float_or_none(previous_status.get("gallons_per_hour"))
            generated = iso_now()
            last_received = received_at or generated
            pump_status = "Not pumping"
            event_type = payload.get("event_type")
            if isinstance(event_type, str) and event_type.lower() != "pump stop":
                pump_status = "Pumping"
            status_payload = {
                "event_type": event_type,
                "pump_run_time_s": run_time,
                "pump_interval_s": interval,
                "gallons_per_hour": gph,
                "last_event_timestamp": payload.get("source_timestamp"),
                "last_received_at": last_received,
                "generated_at": generated,
                "pump_status": pump_status,
            }
            self._write_status_file(self.pump_status_path, status_payload)
        LOGGER.info(
            "Pump event %s @ %s run=%s s interval=%s s",
            payload.get("event_type"),
            payload.get("source_timestamp"),
            payload.get("pump_run_time_s"),
            payload.get("pump_interval_s"),
        )

    def run(self) -> None:
        self.reset_if_needed()
        self.upload_worker.start()

        host = self.env.get("LOCAL_HTTP_HOST", "0.0.0.0")
        port = int(self.env.get("LOCAL_HTTP_PORT", "8080"))
        web_root = repo_path_from_config(self.env.get("WEB_ROOT", "web"))
        start_static_server(web_root, host, port)

        clock = None
        tank_records: Dict[str, List[TVF.DebugSample]] = {}
        pump_events: List[Event] = []
        if self.debug_enabled:
            clock, tank_records, pump_events = self._prepare_debug_inputs()
            if not clock and (
                str_to_bool(self.env.get("DEBUG_TANK"), False)
                or str_to_bool(self.env.get("DEBUG_RELEASER"), False)
            ):
                LOGGER.error("Debug mode enabled but no CSV data was loaded.")

        self._start_tank_processes(self.measurement_params, clock, tank_records)
        self.debug_clock = clock
        self.debug_records = tank_records
        self._start_lcd_process()
        self._start_measurement_collector()
        if pump_events:
            self._start_pump_debug_thread(clock, pump_events)
        elif str_to_bool(self.env.get("DEBUG_RELEASER"), False):
            LOGGER.error("Pump debug enabled but no pump events CSV rows were loaded.")
        if self.debug_enabled:
            self._start_vacuum_debug_thread(clock)
        if not self.debug_enabled:
            LOGGER.info("Tank hardware sampling active; pump automation integration pending.")

        try:
            while not self.stop_event.wait(1):
                self._ensure_tank_processes_alive()
                pass
        finally:
            self.shutdown()


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
        if current_process().name != "MainProcess":
            return
        LOGGER.info("Received signal %s, shutting down.", sig)
        app.shutdown()
        sys.exit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handle_signal)

    app.run()


if __name__ == "__main__":
    main()
