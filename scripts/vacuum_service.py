#!/usr/bin/env python3
"""Vacuum sampling service using cached ADC readings."""
from __future__ import annotations

import logging
import signal
import sys
import threading
from typing import Optional

from adc_cache import cache_age_seconds, read_cache, resolve_cache_path
from config_loader import load_role, repo_path_from_config
from fault_handler import setup_faulthandler
from main_pump import (
    ADC_STALE_SECONDS,
    ERROR_LOG_PATH,
    VACUUM_REFRESH_RATE,
    ADCStaleError,
    LocalErrorWriter,
    PumpDatabase,
    VacuumSampler,
    env_float,
)

LOGGER = logging.getLogger("vacuum_service")


class CachedVacuumReader:
    def __init__(self, cache_path, max_age_seconds: float):
        self.cache_path = cache_path
        self.max_age_seconds = max_age_seconds

    def read_vacuum(self):
        try:
            payload = read_cache(self.cache_path)
        except Exception as exc:
            raise ADCStaleError(f"ADC cache read failed: {exc}") from exc
        age = cache_age_seconds(payload)
        if age > self.max_age_seconds:
            raise ADCStaleError(f"ADC cache stale ({age:.2f}s > {self.max_age_seconds:.2f}s)")
        vacuum = payload.get("vacuum")
        if not isinstance(vacuum, dict):
            raise ADCStaleError("ADC cache missing vacuum data")
        if "inhg" not in vacuum:
            raise ADCStaleError("ADC cache missing vacuum inHg")
        return vacuum


class VacuumService:
    def __init__(self, env):
        db_path = repo_path_from_config(env.get("DB_PATH", "data/pump_pi.db"))
        self.db = PumpDatabase(db_path)
        self.error_writer = LocalErrorWriter(ERROR_LOG_PATH)
        self.refresh_rate = env_float(env, "VACUUM_REFRESH_RATE", VACUUM_REFRESH_RATE)
        stale_seconds = env_float(env, "ADC_STALE_SECONDS", ADC_STALE_SECONDS)
        reader = CachedVacuumReader(resolve_cache_path(env), stale_seconds)
        self.sampler = VacuumSampler(reader, self.db, self.refresh_rate)
        self.stop_event = threading.Event()
        self.watchdog_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self.sampler.start()
        self._start_watchdog()

    def stop(self) -> None:
        self.stop_event.set()
        self.sampler.stop()
        if self.watchdog_thread:
            self.watchdog_thread.join(timeout=2)

    def _start_watchdog(self) -> None:
        if self.watchdog_thread and self.watchdog_thread.is_alive():
            return
        self.watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self.watchdog_thread.start()

    def _watchdog_loop(self) -> None:
        while not self.stop_event.wait(5):
            if not (self.sampler.thread and self.sampler.thread.is_alive()):
                self.error_writer.append("Restarting vacuum loop", source="vacuum_service")
                self.sampler.stop()
                self.sampler.start()


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

    service = VacuumService(env)
    setup_faulthandler("vacuum_service", service.error_writer, service.db)

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
