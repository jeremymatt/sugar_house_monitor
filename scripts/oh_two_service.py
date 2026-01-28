#!/usr/bin/env python3
"""O2 sampling service for MCP3008 channel P0."""
from __future__ import annotations

import csv
import json
import logging
import os
import shutil
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib import error, parse, request

import numpy as np

from config_loader import load_role, repo_path_from_config

LOGGER = logging.getLogger("oh_two")

# Defaults (override in oh_two_pi.env)
SAMPLE_INTERVAL_SECONDS = 5.0
DEBOUNCE_SAMPLES = 8
DEBOUNCE_SAMPLE_DELAY = 0.05
UPLOAD_BATCH_SIZE = 10
UPLOAD_INTERVAL_SECONDS = 5.0
HANDSHAKE_INTERVAL_SECONDS = 60.0
STORAGE_HEARTBEAT_SECONDS = 300.0
ADC_REFERENCE_VOLTAGE = 5.0
CALIBRATION_PATH = repo_path_from_config("scripts/oh_two_cal.csv")
LED_PIN = 23
ERROR_BLINK_HZ = 1.0

try:  # pragma: no cover - hardware dependency
    import RPi.GPIO as GPIO
except ImportError:  # pragma: no cover - non-Pi environment
    GPIO = None


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


class StatusLed:
    def __init__(self, pin: int, blink_hz: float) -> None:
        self.pin = pin
        self.blink_interval = 0.5 if blink_hz <= 0 else 1.0 / (blink_hz * 2)
        self.error_event = threading.Event()
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.enabled = GPIO is not None
        if self.enabled:
            GPIO.setwarnings(False)
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(self.pin, GPIO.OUT)
            GPIO.output(self.pin, GPIO.HIGH)

    def start(self) -> None:
        if not self.enabled:
            return
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def set_error(self, is_error: bool) -> None:
        if not self.enabled:
            return
        if is_error:
            self.error_event.set()
        else:
            self.error_event.clear()

    def stop(self) -> None:
        if not self.enabled:
            return
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)
        GPIO.output(self.pin, GPIO.LOW)
        GPIO.cleanup(self.pin)

    def _run(self) -> None:
        led_on = True
        while not self.stop_event.is_set():
            if self.error_event.is_set():
                led_on = not led_on
                GPIO.output(self.pin, GPIO.HIGH if led_on else GPIO.LOW)
                if self.stop_event.wait(self.blink_interval):
                    break
                continue
            if not led_on:
                led_on = True
                GPIO.output(self.pin, GPIO.HIGH)
            if self.stop_event.wait(0.2):
                break


class O2Reader:
    def __init__(
        self,
        reference_voltage: float,
        calibration_path: Path,
        debounce_samples: int,
        debounce_delay: float,
    ) -> None:
        self.reference_voltage = reference_voltage
        self.calibration_path = calibration_path
        self.debounce_samples = max(1, int(debounce_samples))
        self.debounce_delay = max(0.0, float(debounce_delay))
        self.adc_value_range = (0, 65535)
        self.adc_voltage_range = (0.0, self.reference_voltage)
        self._setup_hardware()
        self._load_calibration()

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
        self.channel = AnalogIn(mcp, MCP.P0)

    def _load_calibration(self) -> None:
        self.calibration = None
        if not self.calibration_path.exists():
            LOGGER.warning(
                "Calibration file %s not found; using linear scale",
                self.calibration_path,
            )
            return
        try:
            with self.calibration_path.open(newline="") as handle:
                reader = csv.reader(handle)
                header = next(reader, None)
                if not header:
                    return
                header_norm = [name.strip().lower() for name in header]
                voltage_idx = header_norm.index("voltage") if "voltage" in header_norm else 0
                value_idx = None
                for key in ("lambda", "vacuum"):
                    if key in header_norm:
                        value_idx = header_norm.index(key)
                        break
                if value_idx is None:
                    if len(header_norm) > 1:
                        value_idx = 1
                    else:
                        LOGGER.warning(
                            "Calibration file %s missing lambda column; using linear scale",
                            self.calibration_path,
                        )
                        return
                rows = []
                for row in reader:
                    if not row or len(row) <= max(voltage_idx, value_idx):
                        continue
                    try:
                        voltage = float(row[voltage_idx])
                        value = float(row[value_idx])
                    except (TypeError, ValueError):
                        continue
                    rows.append((voltage, value))
            if not rows:
                return
            data = np.array(sorted(rows, key=lambda row: row[0]), dtype=float)
            self.calibration = data
        except Exception as exc:  # pragma: no cover
            LOGGER.warning("Failed to load calibration data: %s", exc)
            self.calibration = None

    def _voltage_from_raw(self, raw_value: float) -> float:
        return float(np.interp(raw_value, self.adc_value_range, self.adc_voltage_range))

    def _o2_from_voltage(self, volts: float) -> float:
        if self.calibration is None:
            return float(np.interp(volts, self.adc_voltage_range, (0.0, 100.0)))
        try:
            return float(np.interp(volts, self.calibration[:, 0], self.calibration[:, 1]))
        except Exception:
            return float(np.interp(volts, self.adc_voltage_range, (0.0, 100.0)))

    def read_average(self) -> Dict[str, float]:
        raw_samples = []
        for idx in range(self.debounce_samples):
            raw_samples.append(self.channel.value)
            if idx + 1 < self.debounce_samples and self.debounce_delay > 0:
                time.sleep(self.debounce_delay)
        avg_raw = float(np.mean(raw_samples)) if raw_samples else 0.0
        avg_volts = self._voltage_from_raw(avg_raw)
        o2_lambda = round(self._o2_from_voltage(avg_volts), 3)
        return {
            "raw_value": float(avg_raw),
            "volts": avg_volts,
            "o2_percent": o2_lambda,
        }


class O2Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()
        self._connect()

    def _connect(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            raise RuntimeError(f"Failed to create DB directory {self.path.parent}: {exc}") from exc

        db_exists = self.path.exists()
        try:
            self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        except Exception as exc:
            raise RuntimeError(f"Failed to open DB at {self.path}: {exc}") from exc
        LOGGER.info("Opened O2 DB at %s (%s)", self.path, "existing" if db_exists else "new")

        self.conn.row_factory = sqlite3.Row
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA busy_timeout=5000")
        except Exception as exc:
            LOGGER.warning("Failed to set sqlite pragmas: %s", exc)
        with self.conn:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS o2_readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    o2_percent REAL,
                    raw_value REAL,
                    volts REAL,
                    source_timestamp TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    sent_to_server INTEGER DEFAULT 0,
                    acked_by_server INTEGER DEFAULT 0,
                    UNIQUE(source_timestamp)
                )
                """
            )

    def insert_reading(self, record: Dict[str, object], received_at: Optional[str] = None) -> None:
        payload = {
            "o2_percent": record.get("o2_percent"),
            "raw_value": record.get("raw_value"),
            "volts": record.get("volts"),
            "source_timestamp": record.get("source_timestamp"),
            "received_at": received_at or iso_now(),
        }
        with self.lock, self.conn:
            self.conn.execute(
                """
                INSERT OR IGNORE INTO o2_readings (
                    o2_percent, raw_value, volts, source_timestamp, received_at
                ) VALUES (
                    :o2_percent, :raw_value, :volts, :source_timestamp, :received_at
                )
                """,
                payload,
            )

    def fetch_unsent(self, limit: int) -> List[sqlite3.Row]:
        with self.lock:
            cur = self.conn.execute(
                """
                SELECT *
                FROM o2_readings
                WHERE acked_by_server = 0
                ORDER BY source_timestamp
                LIMIT ?
                """,
                (limit,),
            )
            return cur.fetchall()

    def count_unsent(self) -> int:
        with self.lock:
            cur = self.conn.execute(
                "SELECT COUNT(*) AS pending FROM o2_readings WHERE acked_by_server = 0"
            )
            row = cur.fetchone()
            return int(row["pending"]) if row and row["pending"] is not None else 0

    def mark_acked(self, ids: List[int]) -> None:
        if not ids:
            return
        with self.lock, self.conn:
            self.conn.executemany(
                "UPDATE o2_readings SET sent_to_server=1, acked_by_server=1 WHERE id = ?",
                [(row_id,) for row_id in ids],
            )


class O2Sampler:
    def __init__(self, reader: O2Reader, db: O2Database, sample_interval: float, led: StatusLed) -> None:
        self.reader = reader
        self.db = db
        self.sample_interval = max(0.1, float(sample_interval))
        self.led = led
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
            start = time.monotonic()
            try:
                reading = self.reader.read_average()
                payload = {
                    "o2_percent": reading["o2_percent"],
                    "raw_value": reading["raw_value"],
                    "volts": reading["volts"],
                    "source_timestamp": iso_now(),
                }
                self.db.insert_reading(payload)
                LOGGER.info(
                    "O2 sample: raw=%.0f volts=%.3f lambda=%.3f",
                    reading["raw_value"],
                    reading["volts"],
                    reading["o2_percent"],
                )
                self.led.set_error(False)
            except Exception as exc:  # pragma: no cover - hardware dependency
                LOGGER.exception("O2 sample error: %s", exc)
                self.led.set_error(True)
            elapsed = time.monotonic() - start
            wait_time = max(0.0, self.sample_interval - elapsed)
            if self.stop_event.wait(wait_time):
                break


class UploadWorker:
    def __init__(
        self,
        env: Dict[str, str],
        db: O2Database,
        led: StatusLed,
        sample_interval: float,
    ) -> None:
        self.env = env
        self.db = db
        self.led = led
        self.api_base = env.get("API_BASE_URL", "")
        self.api_key = env.get("API_KEY", "")
        self.batch_size = env_int(env, "UPLOAD_BATCH_SIZE", UPLOAD_BATCH_SIZE)
        upload_raw = env.get("UPLOAD_INTERVAL_SECONDS")
        self.upload_interval = (
            float(sample_interval)
            if upload_raw is None or upload_raw == ""
            else env_float(env, "UPLOAD_INTERVAL_SECONDS", sample_interval)
        )
        self.heartbeat_interval = env_float(env, "HANDSHAKE_INTERVAL_SECONDS", HANDSHAKE_INTERVAL_SECONDS)
        self.storage_heartbeat_interval = env_float(
            env, "STORAGE_HEARTBEAT_SECONDS", STORAGE_HEARTBEAT_SECONDS
        )
        self.disk_usage_path = env.get("DISK_USAGE_PATH", "~")
        self._next_storage_heartbeat = time.monotonic()
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
        next_upload = time.monotonic()
        next_heartbeat = time.monotonic()
        while not self.stop_event.wait(0.5):
            now = time.monotonic()
            if self.upload_interval > 0 and now >= next_upload:
                self._upload_once()
                next_upload = now + self.upload_interval
            if self.heartbeat_interval > 0 and now >= next_heartbeat:
                self._send_heartbeat()
                next_heartbeat = now + self.heartbeat_interval

    def _upload_once(self) -> None:
        pending = self.db.count_unsent()
        if pending > 0:
            LOGGER.info("O2 upload backlog: %s reading(s) pending", pending)
        rows = self.db.fetch_unsent(self.batch_size)
        if not rows:
            return
        readings = []
        ids = []
        for row in rows:
            ids.append(row["id"])
            readings.append(
                {
                    "o2_percent": row["o2_percent"],
                    "raw_value": row["raw_value"],
                    "volts": row["volts"],
                    "source_timestamp": row["source_timestamp"],
                }
            )
        try:
            url = build_url(self.api_base, "ingest_oh_two.php")
            resp = post_json(url, {"readings": readings}, self.api_key)
            if resp.get("status") == "ok":
                self.db.mark_acked(ids)
                self.led.set_error(False)
            else:
                LOGGER.warning("O2 upload failed: %s", resp)
                self.led.set_error(True)
        except error.URLError as exc:
            log_http_error("O2 upload failed", exc)
            self.led.set_error(True)
        except Exception as exc:  # pragma: no cover
            LOGGER.warning("O2 upload failed: %s", exc)
            self.led.set_error(True)

    def _send_heartbeat(self) -> None:
        try:
            url = build_url(self.api_base, "ingest_nodata.php")
            payload = {"stream": "oh_two"}
            payload.update(self._maybe_storage_payload(time.monotonic()))
            resp = post_json(url, payload, self.api_key)
            LOGGER.info("Sent O2 heartbeat (resp=%s)", resp.get("status"))
        except error.URLError as exc:
            log_http_error("O2 heartbeat failed", exc)
        except Exception as exc:  # pragma: no cover
            LOGGER.warning("O2 heartbeat failed: %s", exc)

    def _maybe_storage_payload(self, now: float) -> Dict[str, object]:
        if not self.storage_heartbeat_interval or self.storage_heartbeat_interval <= 0:
            return {}
        if self._next_storage_heartbeat is not None and now < self._next_storage_heartbeat:
            return {}
        self._next_storage_heartbeat = now + self.storage_heartbeat_interval
        return self._disk_usage_payload()

    def _disk_usage_payload(self) -> Dict[str, object]:
        path = os.path.expanduser(self.disk_usage_path or "~")
        try:
            usage = shutil.disk_usage(path)
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.warning("Disk usage read failed for %s: %s", path, exc)
            return {}
        return {
            "disk_total_bytes": usage.total,
            "disk_used_bytes": usage.used,
            "disk_free_bytes": usage.free,
            "disk_path": path,
        }


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        env = load_role("oh_two_pi", required=["API_BASE_URL", "API_KEY", "DB_PATH"])
    except Exception as exc:
        LOGGER.error("Failed to load oh_two_pi env: %s", exc)
        sys.exit(1)

    db_path = repo_path_from_config(env.get("DB_PATH", "data/oh_two_pi.db"))
    db = O2Database(db_path)
    reader = O2Reader(
        reference_voltage=env_float(env, "ADC_REFERENCE_VOLTAGE", ADC_REFERENCE_VOLTAGE),
        calibration_path=repo_path_from_config(env.get("CALIBRATION_PATH", str(CALIBRATION_PATH))),
        debounce_samples=env_int(env, "DEBOUNCE_SAMPLES", DEBOUNCE_SAMPLES),
        debounce_delay=env_float(env, "DEBOUNCE_SAMPLE_DELAY", DEBOUNCE_SAMPLE_DELAY),
    )
    led = StatusLed(env_int(env, "LED_PIN", LED_PIN), ERROR_BLINK_HZ)
    sample_interval = max(0.1, env_float(env, "SAMPLE_INTERVAL_SECONDS", SAMPLE_INTERVAL_SECONDS))
    sampler = O2Sampler(reader, db, sample_interval, led)
    uploader = UploadWorker(env, db, led, sample_interval)

    def handle_signal(sig, frame):
        LOGGER.info("Received signal %s, shutting down.", sig)
        sampler.stop()
        uploader.stop()
        led.stop()
        sys.exit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handle_signal)

    led.start()
    sampler.start()
    uploader.start()

    try:
        while True:
            signal.pause()
    except AttributeError:
        try:
            while True:
                threading.Event().wait(1)
        finally:
            sampler.stop()
            uploader.stop()
            led.stop()


if __name__ == "__main__":
    main()
