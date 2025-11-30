#!/usr/bin/env python3
"""
Pump Pi orchestrator.

Responsibilities:
1) Read MCP3008 channels (P1 tank_full, P2 manual_start, P3 tank_empty) and drive
   a state machine for pumping/manual_pumping/not_pumping.
2) Control the transfer pump relay on GPIO17 (BCM), fail-safe LOW.
3) Upload pump events, vacuum readings, error logs, and heartbeats to the WordPress API.
4) Sample vacuum on MCP3008 P0 using a calibration CSV (unsorted volt/inhg pairs).
"""
from __future__ import annotations

import json
import logging
import math
import queue
import signal
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from urllib import error, parse, request

import numpy as np

from config_loader import load_role, repo_path_from_config

LOGGER = logging.getLogger("pump_pi")

# Config defaults (overridable via pump_pi.env)
ERROR_THRESHOLD = 30  # seconds of continuous error before fatal
LOOP_DELAY = 0.1  # seconds for process 1 and 2 loops (fast input response)
VACUUM_REFRESH_RATE = 10.0  # seconds between vacuum batches
VACUUM_SAMPLES = 10
VACUUM_SAMPLE_DELAY = 0.05  # seconds between individual vacuum samples
ADC_REFERENCE_VOLTAGE = 5.0
ADC_BOOL_THRESHOLD_V = 1.0
ADC_DEBOUNCE_SAMPLES = 3
ADC_DEBOUNCE_DELAY = 0.01  # seconds between debounce samples
PUMP_CONTROL_PIN = 17  # BCM pin for optical relay control
HANDSHAKE_INTERVAL_SECONDS = 150  # pump heartbeat cadence
UPLOAD_BATCH_SIZE = 1
UPLOAD_INTERVAL_SECONDS = 60
VACUUM_UPLOAD_BATCH_SIZE = 8
VACUUM_UPLOAD_INTERVAL_SECONDS = 30
ERROR_UPLOAD_BATCH_SIZE = 8
ERROR_UPLOAD_INTERVAL_SECONDS = 30
ERROR_LOG_PATH = repo_path_from_config("web/tank_error_log.txt")
CALIBRATION_PATH = repo_path_from_config("scripts/vacuum_cal.csv")
DEBUG_SIGNAL_LOG = False


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def env_int(env: Dict[str, str], key: str, default: int) -> int:
    try:
        return int(env.get(key, default))
    except (TypeError, ValueError):
        return default


def env_float(env: Dict[str, str], key: str, default: float) -> float:
    try:
        return float(env.get(key, default))
    except (TypeError, ValueError):
        return default


def env_bool(env: Dict[str, str], key: str, default: bool = False) -> bool:
    raw = env.get(key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


class PumpDatabase:
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
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS error_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    message TEXT NOT NULL,
                    source_timestamp TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    sent_to_server INTEGER DEFAULT 0,
                    acked_by_server INTEGER DEFAULT 0
                )
                """
            )

    def insert_pump_event(self, record: Dict[str, object], received_at: Optional[str] = None) -> None:
        payload = {
            "event_type": record.get("event_type"),
            "source_timestamp": record.get("source_timestamp"),
            "pump_run_time_s": record.get("pump_run_time_s"),
            "pump_interval_s": record.get("pump_interval_s"),
            "gallons_per_hour": record.get("gallons_per_hour"),
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

    def insert_vacuum_reading(self, record: Dict[str, object], received_at: Optional[str] = None) -> None:
        payload = {
            "reading_inhg": record.get("reading_inhg"),
            "source_timestamp": record.get("source_timestamp"),
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

    def insert_error_log(self, record: Dict[str, object], received_at: Optional[str] = None) -> None:
        payload = {
            "source": record.get("source", "pump_pi"),
            "message": record.get("message"),
            "source_timestamp": record.get("source_timestamp") or iso_now(),
            "received_at": received_at or iso_now(),
        }
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT INTO error_logs (
                    source, message, source_timestamp, received_at
                ) VALUES (
                    :source, :message, :source_timestamp, :received_at
                )
                """,
                payload,
            )

    def fetch_unsent(self, table: str, limit: int) -> List[sqlite3.Row]:
        with self.lock:
            cur = self.conn.execute(
                f"""
                SELECT *
                FROM {table}
                WHERE acked_by_server = 0
                ORDER BY source_timestamp
                LIMIT ?
                """,
                (limit,),
            )
            return cur.fetchall()

    def count_unsent(self, table: str) -> int:
        with self.lock:
            cur = self.conn.execute(
                f"""
                SELECT COUNT(*) AS cnt
                FROM {table}
                WHERE acked_by_server = 0
                """
            )
            row = cur.fetchone()
            return int(row["cnt"]) if row else 0

    def mark_acked(self, table: str, ids: List[int]) -> None:
        if not ids:
            return
        with self.lock, self.conn:
            self.conn.executemany(
                f"""
                UPDATE {table}
                SET sent_to_server = 1, acked_by_server = 1
                WHERE id = ?
                """,
                [(row_id,) for row_id in ids],
            )


class LocalErrorWriter:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, message: str, source: str = "pump_pi") -> None:
        line = f"[{iso_now()}] {source}: {message}\n"
        with self.lock:
            with self.path.open("a", encoding="utf-8") as fp:
                fp.write(line)


class MCP3008Reader:
    def __init__(
        self,
        adc_threshold_v: float,
        reference_voltage: float,
        calibration_path: Path,
        debounce_samples: int = ADC_DEBOUNCE_SAMPLES,
        debounce_delay: float = ADC_DEBOUNCE_DELAY,
    ):
        self.adc_threshold_v = adc_threshold_v
        self.reference_voltage = reference_voltage
        self.calibration_path = calibration_path
        self.debounce_samples = max(1, int(debounce_samples))
        self.debounce_delay = max(0.0, float(debounce_delay))
        self._setup_hardware()
        self._load_calibration()
        self.adc_value_range = (0, 65535)
        self.adc_voltage_range = (0.0, self.reference_voltage)

    def _setup_hardware(self) -> None:
        try:
            import busio
            import digitalio
            import board
            import adafruit_mcp3xxx.mcp3008 as MCP
            from adafruit_mcp3xxx.analog_in import AnalogIn
        except ImportError as exc:  # pragma: no cover - hardware dependency
            raise RuntimeError("MCP3008 dependencies not available") from exc

        spi = busio.SPI(clock=board.SCK, MISO=board.MISO, MOSI=board.MOSI)
        cs = digitalio.DigitalInOut(board.D5)
        mcp = MCP.MCP3008(spi, cs)

        # Expose channels we need
        self.channels = {
            "vacuum": AnalogIn(mcp, MCP.P0),
            "tank_full": AnalogIn(mcp, MCP.P1),
            "manual_start": AnalogIn(mcp, MCP.P2),
            "tank_empty": AnalogIn(mcp, MCP.P3),
        }

    def _load_calibration(self) -> None:
        self.calibration = None
        if not self.calibration_path.exists():
            LOGGER.warning("Vacuum calibration file %s not found; vacuum readings will be raw volts", self.calibration_path)
            return
        try:
            data = np.loadtxt(self.calibration_path, delimiter=",", dtype=float, skiprows=1)
            if data.ndim == 1 and data.size == 0:
                return
            data = np.array(sorted(data, key=lambda row: row[0]))
            self.calibration = data
        except Exception as exc:  # pragma: no cover
            LOGGER.warning("Failed to load calibration data: %s", exc)
            self.calibration = None

    def _voltage_from_raw(self, raw_value: int) -> float:
        return float(np.interp(raw_value, self.adc_value_range, self.adc_voltage_range))

    def _pressure_from_voltage(self, voltage: float) -> Optional[float]:
        if self.calibration is None:
            return None
        try:
            return float(np.interp(voltage, self.calibration[:, 0], self.calibration[:, 1]))
        except Exception:
            return None

    def read_vacuum(self) -> Dict[str, float]:
        raw = self.channels["vacuum"].value
        volts = self._voltage_from_raw(raw)
        pressure = self._pressure_from_voltage(volts)
        # Fallback linear map to -29.52..60 if calibration missing, mirroring test_vacuum.py
        if pressure is None:
            pressure = float(np.interp(raw, self.adc_value_range, (-29.52, 60)))
        return {"raw": raw, "volts": volts, "inhg": pressure}

    def read_boolean(self, channel: str) -> bool:
        votes = 0
        for idx in range(self.debounce_samples):
            volts = self._voltage_from_raw(self.channels[channel].value)
            if volts >= self.adc_threshold_v:
                votes += 1
            if idx + 1 < self.debounce_samples and self.debounce_delay > 0:
                time.sleep(self.debounce_delay)
        return votes >= math.ceil(self.debounce_samples / 2)

    def read_signals(self, return_volts: bool = False):
        volts: Dict[str, float] = {}
        signals: Dict[str, bool] = {}
        for key, channel_name in (
            ("tank_full", "tank_full"),
            ("manual_start", "manual_start"),
            ("tank_empty", "tank_empty"),
        ):
            samples = []
            high_votes = 0
            for idx in range(self.debounce_samples):
                raw_volts = self._voltage_from_raw(self.channels[channel_name].value)
                samples.append(raw_volts)
                if raw_volts >= self.adc_threshold_v:
                    high_votes += 1
                if idx + 1 < self.debounce_samples and self.debounce_delay > 0:
                    time.sleep(self.debounce_delay)
            avg_volts = float(np.mean(samples))
            volts[key] = avg_volts
            signals[key] = high_votes >= math.ceil(self.debounce_samples / 2)
        return (signals, volts) if return_volts else signals


@dataclass
class PumpState:
    current_state: str = "not_pumping"
    fatal_error: bool = False
    fatal_sent: bool = False
    pump_start_time: Optional[float] = None
    pump_end_time: Optional[float] = None
    last_stop_time: Optional[float] = None
    last_fill_time: Optional[float] = None
    last_flow_rate: Optional[float] = None
    last_error_message: Optional[str] = None
    last_error_log_time: Optional[float] = None
    error_started_at: Optional[float] = None


class PumpController:
    def __init__(
        self,
        db: PumpDatabase,
        error_writer: LocalErrorWriter,
        error_threshold: int,
        loop_delay: float,
        debug_signal_log: bool = False,
    ):
        self.db = db
        self.error_writer = error_writer
        self.state = PumpState()
        self.error_threshold = error_threshold
        self.loop_delay = loop_delay
        self.debug_signal_log = debug_signal_log
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self._last_signals: Dict[str, bool] = {}
        self._last_signal_log: Optional[Dict[str, bool]] = None
        self._last_signal_log_time: Optional[float] = None

    def start(self, reader: MCP3008Reader) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, args=(reader,), daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)

    def get_state(self) -> PumpState:
        with self.lock:
            return PumpState(**self.state.__dict__)

    def _record_error(self, message: str) -> None:
        now = time.time()
        should_log = False
        if message != self.state.last_error_message:
            should_log = True
            self.state.last_error_message = message
            self.state.last_error_log_time = now
        else:
            last = self.state.last_error_log_time
            if last is None or (now - last) >= 1.0:
                should_log = True
                self.state.last_error_log_time = now
        if not should_log:
            return
        payload = {"source": "pump_pi", "message": message, "source_timestamp": iso_now()}
        self.db.insert_error_log(payload)
        self.error_writer.append(message)

    def _handle_fatal(self) -> None:
        if not self.state.fatal_error:
            self.state.fatal_error = True
            self.state.current_state = "not_pumping"
            self.state.pump_start_time = None
            self.state.last_error_message = "FATAL ERROR: STOPPING"
            self._record_error(self.state.last_error_message)
        if not self.state.fatal_sent:
            payload = {
                "event_type": "Fatal Error",
                "source_timestamp": iso_now(),
                "pump_run_time_s": None,
                "pump_interval_s": None,
                "gallons_per_hour": None,
            }
            self.db.insert_pump_event(payload)
            self.state.fatal_sent = True

    def _increment_error(self, message: Optional[str] = None) -> None:
        now = time.time()
        if self.state.error_started_at is None:
            self.state.error_started_at = now
        if message:
            self._record_error(message)
        if (now - self.state.error_started_at) >= self.error_threshold:
            self._handle_fatal()

    def _reset_error_timer(self) -> None:
        self.state.error_started_at = None

    def _tank_full_event_handling(self, event_type: str) -> None:
        now = time.time()
        if self.state.pump_start_time is None:
            self.state.pump_start_time = now
        fill_time = None
        flow_rate = None
        if self.state.pump_end_time is not None:
            fill_time = max(0.0, now - self.state.pump_end_time)
            if fill_time > 0:
                flow_rate = (12.18 / fill_time) * 3600.0
            else:
                flow_rate = None
            self.state.last_fill_time = fill_time
            self.state.last_flow_rate = flow_rate
            self.state.pump_end_time = None
        else:
            self._record_error("WARNING:tank full & started pumping but no pump_end_time")

        payload = {
            "event_type": event_type,
            "source_timestamp": iso_now(),
            "pump_run_time_s": None,
            "pump_interval_s": fill_time,
            "gallons_per_hour": flow_rate,
        }
        self.db.insert_pump_event(payload)

    def _manual_start(self) -> None:
        now = time.time()
        if self.state.pump_start_time is None:
            self.state.pump_start_time = now
        self.state.pump_end_time = None
        self.state.last_fill_time = None
        self.state.last_flow_rate = None
        payload = {
            "event_type": "Manual Pump Start",
            "source_timestamp": iso_now(),
            "pump_run_time_s": None,
            "pump_interval_s": None,
            "gallons_per_hour": None,
        }
        self.db.insert_pump_event(payload)

    def _pump_stop(self, current_state: str) -> None:
        now = time.time()
        pump_run_time = None
        if self.state.pump_start_time is not None:
            pump_run_time = max(0.0, now - self.state.pump_start_time)
        else:
            self._record_error("WARNING: Missing valid start time for pump event")

        pump_interval = None
        if self.state.last_stop_time is not None:
            pump_interval = max(0.0, now - self.state.last_stop_time)

        payload = {
            "event_type": "Pump Stop",
            "source_timestamp": iso_now(),
            "pump_run_time_s": pump_run_time,
            "pump_interval_s": pump_interval,
            "gallons_per_hour": self.state.last_flow_rate,
        }
        self.db.insert_pump_event(payload)

        self.state.pump_end_time = now
        self.state.last_stop_time = now
        self.state.pump_start_time = None

    def _run(self, reader: MCP3008Reader) -> None:
        while not self.stop_event.is_set():
            try:
                signals, volts = reader.read_signals(return_volts=True)
                self._last_signals = signals
                now = time.time()
                if any(signals.values()) or self.debug_signal_log:
                    if (
                        self._last_signal_log is None
                        or signals != self._last_signal_log
                        or (self._last_signal_log_time is None)
                        or (now - self._last_signal_log_time) >= 1.0
                    ):
                        LOGGER.info(
                            "Signals: tank_full=%s (%.2fv) manual_start=%s (%.2fv) tank_empty=%s (%.2fv)",
                            signals.get("tank_full"),
                            volts.get("tank_full"),
                            signals.get("manual_start"),
                            volts.get("manual_start"),
                            signals.get("tank_empty"),
                            volts.get("tank_empty"),
                        )
                        self._last_signal_log = dict(signals)
                        self._last_signal_log_time = now
                prev_state = self.state.current_state
                self._apply_signals(signals)
                if self.state.current_state != prev_state:
                    LOGGER.info("State transition: %s -> %s", prev_state, self.state.current_state)
            except Exception as exc:  # pragma: no cover
                LOGGER.exception("Signal loop error: %s", exc)
                self._record_error(f"Signal loop error: {exc}")
            self.stop_event.wait(self.loop_delay)

    def _apply_signals(self, signals: Dict[str, bool]) -> None:
        with self.lock:
            if self.state.fatal_error:
                self._handle_fatal()
                return
            if self.state.error_started_at is not None and (time.time() - self.state.error_started_at) >= self.error_threshold:
                self._handle_fatal()
                return

            p1 = bool(signals.get("tank_full"))
            p2 = bool(signals.get("manual_start"))
            p3 = bool(signals.get("tank_empty"))
            state = self.state.current_state

            # 0 0 0
            if not p1 and not p2 and not p3:
                self._reset_error_timer()
                return

            # 1 1 1
            if p1 and p2 and p3:
                self._increment_error(
                    "ERROR: received simultaneous tank empty, manual start, and tank full signals while pumping"
                    if state == "pumping"
                    else (
                        "ERROR: received simultaneous tank empty, manual start, and tank full signals while pumping"
                        if state == "manual_pumping"
                        else "ERROR: received simultaneous tank empty, manual start, and tank full signals while not pumping"
                    )
                )
                if state == "not_pumping":
                    self.state.current_state = "pumping"
                return

            # 1 0 1
            if p1 and not p2 and p3:
                msg = "ERROR: received simultaneous tank empty and tank full signals while pumping"
                if state == "not_pumping":
                    msg = "ERROR: received simultaneous tank empty and tank full signals while not pumping"
                    self.state.current_state = "pumping"
                self._increment_error(msg)
                return

            # 0 1 1
            if (not p1) and p2 and p3:
                msg = "WARNING: received simultaneous tank empty and manual pump start signals while pumping"
                if state == "manual_pumping":
                    msg = "WARNING: received simultaneous tank empty and manual pump start signals while manual pumping"
                if state == "not_pumping":
                    msg = "WARNING: received simultaneous tank empty and manual pump start signals while not pumping"
                self._reset_error_timer()
                self._record_error(msg)
                self.state.current_state = "not_pumping"
                self._pump_stop(state) if state in {"pumping", "manual_pumping"} else None
                return

            # 1 1 0
            if p1 and p2 and not p3:
                if state == "pumping":
                    self._increment_error("WARNING: received simultaneous tank full and manual start signals while auto pumping")
                elif state == "manual_pumping":
                    self._increment_error("WARNING: received simultaneous tank full and manual start signals while manually pumping")
                    self.state.current_state = "pumping"
                    self._tank_full_event_handling("Auto Pump Start")
                elif state == "not_pumping":
                    self._increment_error("WARNING: received simultaneous tank full and manual start signals while not pumping")
                    self.state.current_state = "pumping"
                    self._tank_full_event_handling("Auto Pump Start")
                return

            # 1 0 0
            if p1 and not p2 and not p3:
                if state == "pumping":
                    self._increment_error("WARNING: received tank full signal while manual pumping")
                elif state == "manual_pumping":
                    self._increment_error("WARNING: received tank full signal while manual pumping")
                    self.state.current_state = "pumping"
                    self._tank_full_event_handling("Auto Pump Start")
                elif state == "not_pumping":
                    self._reset_error_timer()
                    self.state.current_state = "pumping"
                    self._tank_full_event_handling("Auto Pump Start")
                return

            # 0 1 0
            if (not p1) and p2 and (not p3):
                if state == "pumping":
                    self._reset_error_timer()
                    self._record_error("WARNING: received manual pump signal while auto pumping")
                elif state == "manual_pumping":
                    self._reset_error_timer()
                elif state == "not_pumping":
                    self._reset_error_timer()
                    self.state.current_state = "manual_pumping"
                    self._manual_start()
                return

            # 0 0 1
            if (not p1) and (not p2) and p3:
                self._reset_error_timer()
                if state in {"pumping", "manual_pumping"}:
                    self.state.current_state = "not_pumping"
                    self._pump_stop(state)
                return

            # Fallback: ensure fatal if needed
            if self.state.error_started_at is not None and (time.time() - self.state.error_started_at) >= self.error_threshold:
                self._handle_fatal()


class PumpRelay:
    def __init__(self, control_pin: int):
        self.control_pin = control_pin
        self.available = True
        try:
            import RPi.GPIO as GPIO
        except ImportError as exc:  # pragma: no cover - hardware dependency
            LOGGER.warning("RPi.GPIO not available; relay control disabled (%s)", exc)
            self.available = False
            self.GPIO = None
            return
        self.GPIO = GPIO
        self.GPIO.setmode(self.GPIO.BCM)
        self.GPIO.setup(self.control_pin, self.GPIO.OUT, initial=self.GPIO.LOW)

    def set_state(self, on: bool) -> None:
        if not self.available or self.GPIO is None:
            return
        try:
            self.GPIO.output(self.control_pin, self.GPIO.HIGH if on else self.GPIO.LOW)
        except Exception as exc:  # pragma: no cover
            LOGGER.warning("Failed to set relay state: %s", exc)

    def cleanup(self) -> None:
        if not self.available or self.GPIO is None:
            return
        try:
            self.GPIO.output(self.control_pin, self.GPIO.LOW)
            self.GPIO.cleanup(self.control_pin)
        except Exception:
            pass


class PumpRelayWorker:
    def __init__(self, relay: PumpRelay, controller: PumpController, loop_delay: float):
        self.relay = relay
        self.controller = controller
        self.loop_delay = loop_delay
        self.thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)
        self.relay.cleanup()

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                state = self.controller.get_state()
                pump_on = (state.current_state in {"pumping", "manual_pumping"}) and not state.fatal_error
                self.relay.set_state(pump_on)
            except Exception as exc:  # pragma: no cover
                LOGGER.exception("Pump relay loop error: %s", exc)
            self.stop_event.wait(self.loop_delay)


class UploadWorker:
    def __init__(self, env: Dict[str, str], db: PumpDatabase):
        self.db = db
        self.api_base = env["API_BASE_URL"]
        self.api_key = env["API_KEY"]
        self.pump_batch = env_int(env, "UPLOAD_BATCH_SIZE", UPLOAD_BATCH_SIZE)
        self.pump_interval = env_int(env, "UPLOAD_INTERVAL_SECONDS", UPLOAD_INTERVAL_SECONDS)
        self.vacuum_batch = env_int(env, "VACUUM_UPLOAD_BATCH_SIZE", VACUUM_UPLOAD_BATCH_SIZE)
        self.vacuum_interval = env_int(env, "VACUUM_UPLOAD_INTERVAL_SECONDS", VACUUM_UPLOAD_INTERVAL_SECONDS)
        self.error_batch = env_int(env, "ERROR_UPLOAD_BATCH_SIZE", ERROR_UPLOAD_BATCH_SIZE)
        self.error_interval = env_int(env, "ERROR_UPLOAD_INTERVAL_SECONDS", ERROR_UPLOAD_INTERVAL_SECONDS)
        self.handshake_interval = env_int(env, "HANDSHAKE_INTERVAL_SECONDS", HANDSHAKE_INTERVAL_SECONDS)
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        now = time.monotonic()
        self._last_pump_handshake = now

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.thread.join(timeout=2)

    def _run(self) -> None:
        next_pump = time.monotonic()
        next_vacuum = time.monotonic()
        next_error = time.monotonic()
        while not self.stop_event.wait(1):
            now = time.monotonic()
            if now >= next_pump:
                sent = self._upload_pump()
                if sent:
                    self._last_pump_handshake = now
                elif now - self._last_pump_handshake >= self.handshake_interval:
                    self._send_handshake()
                    self._last_pump_handshake = now
                next_pump = now + self.pump_interval
            if now >= next_vacuum:
                self._upload_vacuum()
                next_vacuum = now + self.vacuum_interval
            if now >= next_error:
                self._upload_errors()
                next_error = now + self.error_interval

    def _upload_pump(self) -> bool:
        rows = self.db.fetch_unsent("pump_events", self.pump_batch)
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
            LOGGER.info("Uploaded %s pump events (resp=%s)", len(events), resp.get("status"))
            self.db.mark_acked("pump_events", [row["id"] for row in rows])
            remaining = self.db.count_unsent("pump_events")
            if remaining > 0:
                LOGGER.info("Pump queue pending after upload: %s", remaining)
        except error.URLError as exc:
            log_http_error("Pump upload failed", exc)
            return False
        except Exception as exc:  # pragma: no cover
            LOGGER.warning("Pump upload failed: %s", exc)
            return False
        return True

    def _upload_vacuum(self) -> bool:
        rows = self.db.fetch_unsent("vacuum_readings", self.vacuum_batch)
        if not rows:
            return False
        readings = [
            {"reading_inhg": row["reading_inhg"], "source_timestamp": row["source_timestamp"]}
            for row in rows
        ]
        try:
            url = build_url(self.api_base, "ingest_vacuum.php")
            resp = post_json(url, {"readings": readings}, self.api_key)
            LOGGER.info("Uploaded %s vacuum readings (resp=%s)", len(readings), resp.get("status"))
            self.db.mark_acked("vacuum_readings", [row["id"] for row in rows])
            remaining = self.db.count_unsent("vacuum_readings")
            if remaining > 0:
                LOGGER.info("Vacuum queue pending after upload: %s", remaining)
        except error.URLError as exc:
            log_http_error("Vacuum upload failed", exc)
            return False
        except Exception as exc:  # pragma: no cover
            LOGGER.warning("Vacuum upload failed: %s", exc)
            return False
        return True

    def _upload_errors(self) -> bool:
        rows = self.db.fetch_unsent("error_logs", self.error_batch)
        if not rows:
            return False
        records = [
            {
                "timestamp": row["source_timestamp"],
                "source": row["source"],
                "message": row["message"],
            }
            for row in rows
        ]
        try:
            url = build_url(self.api_base, "ingest_error.php")
            resp = post_json(url, {"errors": records}, self.api_key)
            LOGGER.info("Uploaded %s error logs (resp=%s)", len(records), resp.get("status"))
            self.db.mark_acked("error_logs", [row["id"] for row in rows])
            remaining = self.db.count_unsent("error_logs")
            if remaining > 0:
                LOGGER.info("Error-log queue pending after upload: %s", remaining)
        except error.URLError as exc:
            log_http_error("Error-log upload failed", exc)
            return False
        except Exception as exc:  # pragma: no cover
            LOGGER.warning("Error-log upload failed: %s", exc)
            return False
        return True

    def _send_handshake(self) -> None:
        try:
            url = build_url(self.api_base, "ingest_nodata.php")
            payload = {"stream": "pump"}
            resp = post_json(url, payload, self.api_key)
            LOGGER.info("Sent pump heartbeat (resp=%s)", resp.get("status"))
        except error.URLError as exc:
            log_http_error("Pump heartbeat failed", exc)
        except Exception as exc:  # pragma: no cover
            LOGGER.warning("Pump heartbeat failed: %s", exc)


class VacuumSampler:
    def __init__(self, reader: MCP3008Reader, db: PumpDatabase, refresh_rate: float):
        self.reader = reader
        self.db = db
        self.refresh_rate = refresh_rate
        self.thread: Optional[threading.Thread] = None
        self.stop_event = threading.Event()

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)

    def _run(self) -> None:
        while not self.stop_event.is_set():
            try:
                readings = []
                for _ in range(VACUUM_SAMPLES):
                    readings.append(self.reader.read_vacuum())
                    if self.stop_event.wait(VACUUM_SAMPLE_DELAY):
                        break
                if readings:
                    avg_inhg = float(np.mean([r["inhg"] for r in readings]))
                    payload = {"reading_inhg": avg_inhg, "source_timestamp": iso_now()}
                    self.db.insert_vacuum_reading(payload)
            except Exception as exc:  # pragma: no cover
                LOGGER.exception("Vacuum loop error: %s", exc)
            self.stop_event.wait(self.refresh_rate)


class PumpApp:
    def __init__(self, env: Dict[str, str]):
        self.env = env
        db_path = repo_path_from_config(env.get("DB_PATH", "data/pump_pi.db"))
        self.db = PumpDatabase(db_path)
        self.error_writer = LocalErrorWriter(ERROR_LOG_PATH)
        self.error_threshold = env_int(env, "ERROR_THRESHOLD", ERROR_THRESHOLD)
        self.loop_delay = env_float(env, "LOOP_DELAY", LOOP_DELAY)
        self.vacuum_refresh_rate = env_float(env, "VACUUM_REFRESH_RATE", VACUUM_REFRESH_RATE)
        self.pump_control_pin = env_int(env, "PUMP_CONTROL_PIN", PUMP_CONTROL_PIN)
        debug_signal_log = env_bool(env, "DEBUG_SIGNAL_LOG", DEBUG_SIGNAL_LOG)
        self.reader = MCP3008Reader(
            adc_threshold_v=env_float(env, "ADC_BOOL_THRESHOLD_V", ADC_BOOL_THRESHOLD_V),
            reference_voltage=env_float(env, "ADC_REFERENCE_VOLTAGE", ADC_REFERENCE_VOLTAGE),
            calibration_path=repo_path_from_config(env.get("VACUUM_CAL_PATH", str(CALIBRATION_PATH))),
            debounce_samples=env_int(env, "ADC_DEBOUNCE_SAMPLES", ADC_DEBOUNCE_SAMPLES),
            debounce_delay=env_float(env, "ADC_DEBOUNCE_DELAY", ADC_DEBOUNCE_DELAY),
        )
        self.controller = PumpController(
            db=self.db,
            error_writer=self.error_writer,
            error_threshold=self.error_threshold,
            loop_delay=self.loop_delay,
            debug_signal_log=debug_signal_log,
        )
        self.relay = PumpRelay(self.pump_control_pin)
        self.relay_worker = PumpRelayWorker(self.relay, self.controller, self.loop_delay)
        self.uploader = UploadWorker(env, self.db)
        self.vacuum_sampler = VacuumSampler(self.reader, self.db, self.vacuum_refresh_rate)
        self.stop_event = threading.Event()
        self.watchdog_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self.controller.start(self.reader)
        self.relay_worker.start()
        self.vacuum_sampler.start()
        self.uploader.start()
        self._start_watchdog()

    def shutdown(self) -> None:
        self.stop_event.set()
        self.controller.stop()
        self.relay_worker.stop()
        self.vacuum_sampler.stop()
        self.uploader.stop()
        if self.watchdog_thread:
            self.watchdog_thread.join(timeout=2)

    def _start_watchdog(self) -> None:
        if self.watchdog_thread and self.watchdog_thread.is_alive():
            return
        self.watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self.watchdog_thread.start()

    def _restart_thread(self, target: str) -> None:
        self.error_writer.append(f"Restarting {target} loop")
        if target == "controller":
            self.controller.stop()
            self.controller.start(self.reader)
        elif target == "relay":
            self.relay_worker.stop()
            self.relay_worker.start()
        elif target == "vacuum":
            self.vacuum_sampler.stop()
            self.vacuum_sampler.start()

    def _watchdog_loop(self) -> None:
        while not self.stop_event.wait(5):
            if not (self.controller.thread and self.controller.thread.is_alive()):
                self._restart_thread("controller")
            if not (self.relay_worker.thread and self.relay_worker.thread.is_alive()):
                self._restart_thread("relay")
            if not (self.vacuum_sampler.thread and self.vacuum_sampler.thread.is_alive()):
                self._restart_thread("vacuum")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        env = load_role("pump_pi", required=["API_BASE_URL", "API_KEY"])
    except Exception as exc:
        LOGGER.error("Failed to load pump_pi env: %s", exc)
        sys.exit(1)

    app = PumpApp(env)

    def handle_signal(sig, frame):
        LOGGER.info("Received signal %s, shutting down.", sig)
        app.shutdown()
        sys.exit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handle_signal)

    app.start()

    try:
        while True:
            time.sleep(1)
    finally:
        app.shutdown()


if __name__ == "__main__":
    main()
