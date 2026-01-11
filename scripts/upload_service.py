#!/usr/bin/env python3
"""Upload worker service for pump/vacuum/error logs."""
from __future__ import annotations

import logging
import signal
import sys
import threading
from typing import Optional

from config_loader import load_role, repo_path_from_config
from fault_handler import setup_faulthandler
from main_pump import (
    ERROR_LOG_PATH,
    UploadWorker,
    LocalErrorWriter,
    PumpDatabase,
)

LOGGER = logging.getLogger("upload_service")


class UploadService:
    def __init__(self, env):
        self.env = env
        db_path = repo_path_from_config(env.get("DB_PATH", "data/pump_pi.db"))
        self.db = PumpDatabase(db_path)
        self.error_writer = LocalErrorWriter(ERROR_LOG_PATH)
        self.worker = UploadWorker(self.env, self.db)
        self.stop_event = threading.Event()
        self.watchdog_thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self.worker.start()
        self._start_watchdog()

    def stop(self) -> None:
        self.stop_event.set()
        self.worker.stop()
        if self.watchdog_thread:
            self.watchdog_thread.join(timeout=2)

    def _start_watchdog(self) -> None:
        if self.watchdog_thread and self.watchdog_thread.is_alive():
            return
        self.watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self.watchdog_thread.start()

    def _watchdog_loop(self) -> None:
        while not self.stop_event.wait(5):
            if not (self.worker.thread and self.worker.thread.is_alive()):
                self.error_writer.append("Restarting upload loop", source="upload_service")
                self.worker.stop()
                self.worker = UploadWorker(self.env, self.db)
                self.worker.start()


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

    service = UploadService(env)
    setup_faulthandler("upload_service", service.error_writer, service.db)

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
