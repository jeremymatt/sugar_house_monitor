#!/usr/bin/env python3
"""ADC service that owns MCP3008 and publishes a cached snapshot."""
from __future__ import annotations

import logging
import math
import signal
import sys
import threading
import time
from collections import deque
from typing import Dict

import numpy as np

from adc_cache import resolve_cache_path, write_cache
from config_loader import load_role, repo_path_from_config
from fault_handler import setup_faulthandler
from main_pump import (
    ADC_BOOL_THRESHOLD_V,
    ADC_DEBOUNCE_DELAY,
    ADC_DEBOUNCE_SAMPLES,
    ADC_REFERENCE_VOLTAGE,
    ERROR_LOG_PATH,
    MCP3008Reader,
    LocalErrorWriter,
    PumpDatabase,
    env_float,
    env_int,
    iso_now,
)

LOGGER = logging.getLogger("adc_service")


def _compute_signal(raw_values: deque[int], reader: MCP3008Reader, threshold_v: float) -> tuple[bool, float]:
    if not raw_values:
        return False, 0.0
    volts = [reader._voltage_from_raw(raw) for raw in raw_values]
    avg_volts = float(np.mean(volts)) if volts else 0.0
    votes = sum(1 for v in volts if v >= threshold_v)
    signal = votes >= math.ceil(len(volts) / 2)
    return signal, avg_volts


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

    db_path = repo_path_from_config(env.get("DB_PATH", "data/pump_pi.db"))
    db = PumpDatabase(db_path)
    error_writer = LocalErrorWriter(ERROR_LOG_PATH)
    setup_faulthandler("adc_service", error_writer, db)

    cache_path = resolve_cache_path(env)
    sample_interval = env_float(env, "ADC_SAMPLE_INTERVAL_SECONDS", 0.02)
    sample_hz = env_float(env, "ADC_SAMPLE_HZ", 0.0)
    if sample_hz and sample_hz > 0:
        sample_interval = max(0.001, 1.0 / sample_hz)

    threshold_v = env_float(env, "ADC_BOOL_THRESHOLD_V", ADC_BOOL_THRESHOLD_V)
    reference_voltage = env_float(env, "ADC_REFERENCE_VOLTAGE", ADC_REFERENCE_VOLTAGE)
    debounce_samples = env_int(env, "ADC_DEBOUNCE_SAMPLES", ADC_DEBOUNCE_SAMPLES)
    debounce_delay = env_float(env, "ADC_DEBOUNCE_DELAY", ADC_DEBOUNCE_DELAY)

    try:
        reader = MCP3008Reader(
            adc_threshold_v=threshold_v,
            reference_voltage=reference_voltage,
            calibration_path=repo_path_from_config(env.get("VACUUM_CAL_PATH", "scripts/vacuum_cal.csv")),
            debounce_samples=debounce_samples,
            debounce_delay=debounce_delay,
        )
    except Exception as exc:
        LOGGER.error("Failed to initialize MCP3008: %s", exc)
        sys.exit(1)

    bool_buffers: Dict[str, deque[int]] = {
        name: deque(maxlen=max(1, debounce_samples))
        for name in ("tank_full", "manual_start", "tank_empty")
    }

    stop_event = threading.Event()

    def handle_signal(sig, frame):
        LOGGER.info("Received signal %s, shutting down.", sig)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handle_signal)

    LOGGER.info("ADC cache path: %s", cache_path)

    while not stop_event.wait(sample_interval):
        try:
            raw = {
                "vacuum": reader.channels["vacuum"].value,
                "tank_full": reader.channels["tank_full"].value,
                "manual_start": reader.channels["manual_start"].value,
                "tank_empty": reader.channels["tank_empty"].value,
            }
            for key in ("tank_full", "manual_start", "tank_empty"):
                bool_buffers[key].append(raw[key])

            signals = {}
            volts = {}
            for key in ("tank_full", "manual_start", "tank_empty"):
                signal_value, avg_volts = _compute_signal(bool_buffers[key], reader, threshold_v)
                signals[key] = signal_value
                volts[key] = avg_volts

            vacuum_raw = raw["vacuum"]
            vacuum_volts = reader._voltage_from_raw(vacuum_raw)
            vacuum_inhg = reader._pressure_from_voltage(vacuum_volts)
            if vacuum_inhg is None:
                vacuum_inhg = float(np.interp(vacuum_raw, reader.adc_value_range, (-29.52, 60)))

            payload = {
                "monotonic": time.monotonic(),
                "timestamp": iso_now(),
                "signals": signals,
                "volts": volts,
                "raw": {
                    "tank_full": raw["tank_full"],
                    "manual_start": raw["manual_start"],
                    "tank_empty": raw["tank_empty"],
                },
                "vacuum": {
                    "raw": vacuum_raw,
                    "volts": vacuum_volts,
                    "inhg": vacuum_inhg,
                },
            }
            write_cache(cache_path, payload)
        except Exception as exc:
            LOGGER.exception("ADC loop error: %s", exc)
            try:
                message = f"ADC loop error: {exc}"
                error_writer.append(message, source="adc_service")
                db.insert_error_log(
                    {
                        "source": "adc_service",
                        "message": message,
                        "source_timestamp": iso_now(),
                    }
                )
            except Exception:
                pass


if __name__ == "__main__":
    main()
