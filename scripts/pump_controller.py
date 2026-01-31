#!/usr/bin/env python3
"""Pump controller service that reads cached ADC signals and drives the relay."""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from typing import Dict, Optional

from adc_cache import cache_age_seconds, read_cache, resolve_cache_path
from config_loader import load_role, repo_path_from_config
from fault_handler import setup_faulthandler
from main_pump import (
    ADC_STALE_FATAL_SECONDS,
    ADC_STALE_SECONDS,
    CONTROL_HOLD_SECONDS,
    DEBUG_SIGNAL_LOG,
    ERROR_LOG_PATH,
    ERROR_THRESHOLD,
    FatalPrefixFilter,
    LOOP_DELAY,
    PUMP_CONTROL_PIN,
    ADCStaleError,
    LocalErrorWriter,
    PumpController,
    PumpDatabase,
    PumpRelay,
    PumpRelayWorker,
    env_bool,
    env_float,
    env_int,
)

LOGGER = logging.getLogger("pump_controller")

# Pump state cache for LED controller
PUMP_STATE_CACHE_PATH = "/dev/shm/pump_state_cache.json"


class CachedSignalReader:
    def __init__(self, cache_path, max_age_seconds: float):
        self.cache_path = cache_path
        self.max_age_seconds = max_age_seconds

    def read_signals(self, return_volts: bool = False):
        try:
            payload = read_cache(self.cache_path)
        except Exception as exc:
            raise ADCStaleError(f"ADC cache read failed: {exc}") from exc
        age = cache_age_seconds(payload)
        if age > self.max_age_seconds:
            raise ADCStaleError(f"ADC cache stale ({age:.2f}s > {self.max_age_seconds:.2f}s)")
        signals = payload.get("signals")
        volts = payload.get("volts")
        if not isinstance(signals, dict):
            raise ADCStaleError("ADC cache missing signals")
        if not isinstance(volts, dict):
            raise ADCStaleError("ADC cache missing volts")
        for key in ("tank_full", "manual_start", "tank_empty"):
            if key not in volts:
                raise ADCStaleError(f"ADC cache missing volts for {key}")
        for key in ("service_on", "service_off", "clear_fatal"):
            signals.setdefault(key, False)
            volts.setdefault(key, 0.0)
        vacuum = payload.get("vacuum")
        if isinstance(vacuum, dict) and vacuum.get("volts") is not None:
            volts["vacuum"] = vacuum.get("volts")
        return (signals, volts) if return_volts else signals


class PumpStateCacheWriter:
    """Writes pump controller state to shared memory cache for LED controller."""

    def __init__(self, cache_path: str):
        """Initialize cache writer.

        Args:
            cache_path: Path to write pump state cache JSON file
        """
        self.cache_path = cache_path

    def write_state(self, state, adc_stale_fatal_seconds: float) -> None:
        """Write pump state to cache file (atomic write).

        Args:
            state: PumpState object from controller
            adc_stale_fatal_seconds: Fatal threshold for ADC staleness
        """
        payload = {
            "timestamp": time.time(),
            "current_state": state.current_state,
            "fatal_error": state.fatal_error,
            "adc_stale_started_at": state.adc_stale_started_at,
            "adc_stale_fatal_seconds": adc_stale_fatal_seconds,
        }

        # Atomic write: write to temp file, then rename
        temp_path = f"{self.cache_path}.tmp"
        try:
            with open(temp_path, "w") as f:
                json.dump(payload, f)
            os.replace(temp_path, self.cache_path)  # Atomic on POSIX
        except Exception as exc:
            LOGGER.warning("Failed to write pump state cache: %s", exc)


class ControllerService:
    def __init__(self, env: Dict[str, str]):
        db_path = repo_path_from_config(env.get("DB_PATH", "data/pump_pi.db"))
        self.db = PumpDatabase(db_path)
        self.error_writer = LocalErrorWriter(ERROR_LOG_PATH)
        self.loop_delay = env_float(env, "LOOP_DELAY", LOOP_DELAY)
        self.error_threshold = env_int(env, "ERROR_THRESHOLD", ERROR_THRESHOLD)
        self.adc_stale_seconds = env_float(env, "ADC_STALE_SECONDS", ADC_STALE_SECONDS)
        self.adc_stale_fatal_seconds = env_float(
            env, "ADC_STALE_FATAL_SECONDS", ADC_STALE_FATAL_SECONDS
        )
        self.control_hold_seconds = env_float(
            env, "CONTROL_HOLD_SECONDS", CONTROL_HOLD_SECONDS
        )
        self.pump_control_pin = env_int(env, "PUMP_CONTROL_PIN", PUMP_CONTROL_PIN)
        self.reader = CachedSignalReader(resolve_cache_path(env), self.adc_stale_seconds)
        verbose_log = env_bool(env, "VERBOSE", False)
        debug_signal_log = verbose_log or env_bool(env, "DEBUG_SIGNAL_LOG", DEBUG_SIGNAL_LOG)
        self.controller = PumpController(
            db=self.db,
            error_writer=self.error_writer,
            error_threshold=self.error_threshold,
            loop_delay=self.loop_delay,
            adc_stale_fatal_seconds=self.adc_stale_fatal_seconds,
            control_hold_seconds=self.control_hold_seconds,
            debug_signal_log=debug_signal_log,
        )
        self.relay = PumpRelay(self.pump_control_pin)
        self.relay_worker = PumpRelayWorker(self.relay, self.controller, self.loop_delay)

        # State cache writer for LED controller
        self.state_cache_writer = PumpStateCacheWriter(PUMP_STATE_CACHE_PATH)
        # Attach to relay worker so it can write cache in its loop
        self.relay_worker.state_cache_writer = self.state_cache_writer
        self.relay_worker.adc_stale_fatal_seconds = self.adc_stale_fatal_seconds

        self.stop_event = threading.Event()
        self.watchdog_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self.controller.start(self.reader)
        self.relay_worker.start()
        self._start_watchdog()

    def stop(self) -> None:
        self.stop_event.set()
        self.controller.stop()
        self.relay_worker.stop()
        if self.watchdog_thread:
            self.watchdog_thread.join(timeout=2)

    def _start_watchdog(self) -> None:
        if self.watchdog_thread and self.watchdog_thread.is_alive():
            return
        self.watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self.watchdog_thread.start()

    def _append_error(self, message: str) -> None:
        if self.controller.get_state().fatal_error and not message.startswith("[FATAL ERROR]"):
            message = f"[FATAL ERROR] {message}"
        self.error_writer.append(message, source="pump_controller")

    def _watchdog_loop(self) -> None:
        while not self.stop_event.wait(5):
            if not (self.controller.thread and self.controller.thread.is_alive()):
                self._append_error("Restarting controller loop")
                self.controller.stop()
                self.controller.start(self.reader)
            if not (self.relay_worker.thread and self.relay_worker.thread.is_alive()):
                self._append_error("Restarting relay loop")
                self.relay_worker.stop()
                self.relay_worker.start()


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

    service = ControllerService(env)
    root_logger = logging.getLogger()
    for handler in root_logger.handlers:
        handler.addFilter(FatalPrefixFilter(service.controller))
    setup_faulthandler("pump_controller", service.error_writer, service.db)

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
        # signal.pause is not available on all platforms; fallback to sleep loop.
        try:
            while True:
                threading.Event().wait(1)
        finally:
            service.stop()


if __name__ == "__main__":
    main()
