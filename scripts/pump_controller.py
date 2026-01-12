#!/usr/bin/env python3
"""Pump controller service that reads cached ADC signals and drives the relay."""
from __future__ import annotations

import logging
import signal
import sys
import threading
from typing import Dict, Optional

from adc_cache import cache_age_seconds, read_cache, resolve_cache_path
from config_loader import load_role, repo_path_from_config
from fault_handler import setup_faulthandler
from main_pump import (
    ADC_STALE_FATAL_SECONDS,
    ADC_STALE_SECONDS,
    DEBUG_SIGNAL_LOG,
    ERROR_LOG_PATH,
    ERROR_THRESHOLD,
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
        return (signals, volts) if return_volts else signals


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
            debug_signal_log=debug_signal_log,
        )
        self.relay = PumpRelay(self.pump_control_pin)
        self.relay_worker = PumpRelayWorker(self.relay, self.controller, self.loop_delay)
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

    def _watchdog_loop(self) -> None:
        while not self.stop_event.wait(5):
            if not (self.controller.thread and self.controller.thread.is_alive()):
                self.error_writer.append("Restarting controller loop", source="pump_controller")
                self.controller.stop()
                self.controller.start(self.reader)
            if not (self.relay_worker.thread and self.relay_worker.thread.is_alive()):
                self.error_writer.append("Restarting relay loop", source="pump_controller")
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
