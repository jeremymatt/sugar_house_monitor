#!/usr/bin/env python3
"""ADC watchdog that re-enables pump services on a rising service_on edge."""
from __future__ import annotations

import logging
import signal
import subprocess
import sys
import threading
import time
from typing import Dict, Optional

from adc_cache import cache_age_seconds, read_cache, resolve_cache_path
from config_loader import load_role, repo_path_from_config
from fault_handler import setup_faulthandler
from main_pump import (
    ADC_STALE_SECONDS,
    CONTROL_HOLD_SECONDS,
    ERROR_LOG_PATH,
    LOOP_DELAY,
    ADCStaleError,
    LocalErrorWriter,
    PumpDatabase,
    env_float,
    iso_now,
)

LOGGER = logging.getLogger("adc_watchdog")


class CachedSignalReader:
    def __init__(self, cache_path, max_age_seconds: float):
        self.cache_path = cache_path
        self.max_age_seconds = max_age_seconds

    def read_signals(self) -> Dict[str, bool]:
        try:
            payload = read_cache(self.cache_path)
        except Exception as exc:
            raise ADCStaleError(f"ADC cache read failed: {exc}") from exc
        age = cache_age_seconds(payload)
        if age > self.max_age_seconds:
            raise ADCStaleError(f"ADC cache stale ({age:.2f}s > {self.max_age_seconds:.2f}s)")
        signals = payload.get("signals")
        if not isinstance(signals, dict):
            raise ADCStaleError("ADC cache missing signals")
        return {
            "service_on": bool(signals.get("service_on")),
        }


class ADCWatchdogService:
    def __init__(self, env: Dict[str, str]):
        db_path = repo_path_from_config(env.get("DB_PATH", "data/pump_pi.db"))
        self.db = PumpDatabase(db_path)
        self.error_writer = LocalErrorWriter(ERROR_LOG_PATH)
        stale_seconds = env_float(env, "ADC_STALE_SECONDS", ADC_STALE_SECONDS)
        self.reader = CachedSignalReader(resolve_cache_path(env), stale_seconds)
        self.loop_delay = env_float(env, "WATCHDOG_LOOP_DELAY", LOOP_DELAY)
        self.control_hold_seconds = max(
            0.0, env_float(env, "CONTROL_HOLD_SECONDS", CONTROL_HOLD_SECONDS)
        )
        self.systemd_setup_path = repo_path_from_config("scripts/pump_pi_setup/systemd_setup.sh")
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self._hold_start: Optional[float] = None
        self._hold_fired = False
        self._last_stale_log = 0.0
        self._systemd_lock = threading.Lock()

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

    def _record_error(self, message: str) -> None:
        payload = {"source": "adc_watchdog", "message": message, "source_timestamp": iso_now()}
        try:
            self.db.insert_error_log(payload)
        except Exception as exc:
            LOGGER.warning("Failed to persist watchdog error log: %s", exc)
        try:
            self.error_writer.append(message, source="adc_watchdog")
        except Exception as exc:
            LOGGER.warning("Failed to write watchdog error log: %s", exc)

    def _spawn_systemd_setup(self, mode: str) -> None:
        threading.Thread(
            target=self._run_systemd_setup,
            args=(mode,),
            daemon=True,
        ).start()

    def _run_systemd_setup(self, mode: str) -> None:
        if not self._systemd_lock.acquire(blocking=False):
            LOGGER.info("Skipping systemd_setup.sh -%s; command already running", mode)
            return
        try:
            script_path = self.systemd_setup_path
            if not script_path.exists():
                self._record_error(f"systemd_setup.sh not found at {script_path}")
                return
            cmd = ["sudo", "-n", "systemd-run", "--scope", "--quiet", str(script_path), f"-{mode}"]
            try:
                result = subprocess.run(cmd, capture_output=True, text=True)
            except Exception as exc:
                self._record_error(f"Failed to run systemd_setup.sh -{mode}: {exc}")
                return
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()
                suffix = f": {detail}" if detail else ""
                self._record_error(
                    f"systemd_setup.sh -{mode} failed (code {result.returncode}){suffix}"
                )
        finally:
            self._systemd_lock.release()

    def _run(self) -> None:
        while not self.stop_event.wait(self.loop_delay):
            try:
                signals = self.reader.read_signals()
                service_on = bool(signals.get("service_on"))
                now = time.monotonic()
                if service_on:
                    if self._hold_start is None:
                        self._hold_start = now
                    if (
                        not self._hold_fired
                        and (now - self._hold_start) >= self.control_hold_seconds
                    ):
                        self._spawn_systemd_setup("on")
                        self._hold_fired = True
                else:
                    self._hold_start = None
                    self._hold_fired = False
            except ADCStaleError as exc:
                now = time.time()
                if (now - self._last_stale_log) >= 1.0:
                    LOGGER.warning("ADC data stale: %s", exc)
                    self._last_stale_log = now
            except Exception as exc:  # pragma: no cover
                LOGGER.exception("Watchdog loop error: %s", exc)
                self._record_error(f"Watchdog loop error: {exc}")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        env = load_role("pump_pi")
    except Exception as exc:
        LOGGER.error("Failed to load pump_pi env: %s", exc)
        sys.exit(1)

    service = ADCWatchdogService(env)
    setup_faulthandler("adc_watchdog", service.error_writer, service.db)

    def handle_signal(sig, frame):
        LOGGER.info("Received signal %s, shutting down.", sig)
        service.stop()
        sys.exit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handle_signal)

    service.start()

    try:
        while True:
            signal.pause()
    except AttributeError:
        try:
            while True:
                threading.Event().wait(1)
        finally:
            service.stop()


if __name__ == "__main__":
    main()
