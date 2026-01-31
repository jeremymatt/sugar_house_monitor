#!/usr/bin/env python3
"""LED status indicator service for pump controller.

This service reads pump state from shared memory cache and displays
status on a dual-color (red/green) LED indicator.
"""
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Dict, Optional

from config_loader import load_role
from main_pump import env_float, env_int
from status_led import StatusLED

LOGGER = logging.getLogger("led_controller")


class CacheStaleError(Exception):
    """Raised when state cache is stale or unreadable."""
    pass


class PumpStateCacheReader:
    """Reads pump controller state from shared memory cache.

    Attributes:
        cache_path: Path to the pump state cache JSON file
        max_age_seconds: Maximum acceptable cache age before considering stale
    """

    def __init__(self, cache_path: str, max_age_seconds: float):
        """Initialize cache reader.

        Args:
            cache_path: Path to pump state cache JSON file
            max_age_seconds: Maximum cache age threshold (seconds)
        """
        self.cache_path = cache_path
        self.max_age_seconds = max_age_seconds

    def read_state(self) -> dict:
        """Read and validate pump state from cache.

        Returns:
            dict: Pump state payload from cache

        Raises:
            CacheStaleError: If cache is missing, corrupt, or too old
        """
        try:
            with open(self.cache_path, "r") as f:
                payload = json.load(f)
        except FileNotFoundError:
            raise CacheStaleError(f"Cache file not found: {self.cache_path}")
        except json.JSONDecodeError as exc:
            raise CacheStaleError(f"Cache JSON corrupt: {exc}")
        except Exception as exc:
            raise CacheStaleError(f"Cache read failed: {exc}")

        # Validate timestamp exists
        if "timestamp" not in payload:
            raise CacheStaleError("Cache missing timestamp field")

        # Check cache age
        age = time.time() - payload["timestamp"]
        if age > self.max_age_seconds:
            raise CacheStaleError(f"Cache stale: {age:.2f}s > {self.max_age_seconds:.2f}s")

        return payload

    def cache_age(self) -> float:
        """Get age of cache file in seconds.

        Returns:
            float: Cache age in seconds, or float('inf') if file missing/unreadable
        """
        try:
            with open(self.cache_path, "r") as f:
                payload = json.load(f)
            if "timestamp" in payload:
                return time.time() - payload["timestamp"]
        except Exception:
            pass
        return float('inf')


class LEDControllerService:
    """Main LED controller service.

    Reads pump state from cache and updates LED display based on priority:
    1. Pump service down -> Red solid
    2. Cache stale -> Alternating red/green
    3. Fatal error -> Red blink 2Hz
    4. ADC stale warning -> Red blink 1Hz
    5. Manual pumping -> Green blink 2Hz
    6. Auto pumping -> Green blink 1Hz
    7. Not pumping (idle) -> Green solid
    8. Unknown/startup -> Both OFF
    """

    def __init__(self, env: Dict[str, str]):
        """Initialize LED controller service.

        Args:
            env: Environment configuration dict
        """
        # Initialize LED - both pins set LOW for safety
        self.led = StatusLED(
            env_int(env, "STATUS_LED_RED_PIN", 21),
            env_int(env, "STATUS_LED_GREEN_PIN", 20)
        )

        # Initialize cache reader
        self.cache_reader = PumpStateCacheReader(
            "/dev/shm/pump_state_cache.json",
            env_float(env, "LED_CACHE_STALE_SECONDS", 5.0)
        )

        # Timing configuration
        self.loop_delay = env_float(env, "LED_LOOP_DELAY", 0.05)  # 50ms for smooth blink

        # Blink rates (Hz)
        self.blink_rates = {
            "fatal_error": env_float(env, "LED_BLINK_RATE_FATAL", 2.0),
            "adc_stale": env_float(env, "LED_BLINK_RATE_STALE", 1.0),
            "pumping": env_float(env, "LED_BLINK_RATE_AUTO", 1.0),
            "manual_pumping": env_float(env, "LED_BLINK_RATE_MANUAL", 2.0),
            "alternating": env_float(env, "LED_ALTERNATING_RATE", 1.0),
        }

        # State tracking
        self.stop_event = threading.Event()
        self.blink_start_time = time.monotonic()

    def run(self) -> None:
        """Main service loop - runs until stop_event is set."""
        LOGGER.info("LED controller service starting")
        while not self.stop_event.wait(self.loop_delay):
            try:
                self._update_led()
            except Exception as exc:
                LOGGER.error("LED update loop error: %s", exc, exc_info=True)
                # On unexpected error, show alternating pattern (indicates malfunction)
                try:
                    self._display_alternating()
                except Exception:
                    pass  # If even alternating fails, just continue

        LOGGER.info("LED controller service stopped")

    def _update_led(self) -> None:
        """Update LED based on current system state (priority-based)."""
        # Priority 1: Check if pump service is running
        if not self._pump_service_running():
            self.led.set_solid("red")
            return

        # Priority 2: Check cache staleness
        try:
            cache_age = self.cache_reader.cache_age()
            if cache_age > self.cache_reader.max_age_seconds:
                # Cache stale - show alternating pattern
                self._display_alternating()
                return

            # Read pump state from cache
            state = self.cache_reader.read_state()

        except Exception as exc:
            # Cache read error - show alternating pattern
            self._display_alternating()
            return

        # Priority 3-7: Display based on pump state
        self._display_pump_state(state)

    def _pump_service_running(self) -> bool:
        """Check if pump controller service is running via systemctl.

        Returns:
            bool: True if service is active, False otherwise
        """
        try:
            result = subprocess.run(
                ["systemctl", "is-active", "--quiet", "sugar-pump-controller"],
                capture_output=True,
                timeout=1.0
            )
            return result.returncode == 0
        except Exception as exc:
            LOGGER.warning("Failed to check pump service status: %s", exc)
            # Fallback: assume running if we can't check
            return True

    def _display_alternating(self) -> None:
        """Display alternating red/off/green/off pattern (cache stale indication).

        Pattern: Red (250ms) → OFF (250ms) → Green (250ms) → OFF (250ms) → repeat
        Cycle duration: 1 second (1Hz)
        """
        rate = self.blink_rates["alternating"]
        cycle_duration = 1.0 / rate  # 1 second at 1Hz
        elapsed = time.monotonic() - self.blink_start_time
        phase = (elapsed % cycle_duration) / cycle_duration  # 0.0 to 1.0

        # 4 phases in cycle
        if phase < 0.25:
            self.led.set_solid("red")
        elif phase < 0.5:
            self.led.set_solid("off")
        elif phase < 0.75:
            self.led.set_solid("green")
        else:
            self.led.set_solid("off")

    def _display_pump_state(self, state: dict) -> None:
        """Display LED based on pump state from cache.

        Priority order:
        3. Fatal error -> Red blink 2Hz
        4. ADC stale warning -> Red blink 1Hz
        5. Manual pumping -> Green blink 2Hz
        6. Auto pumping -> Green blink 1Hz
        7. Not pumping -> Green solid
        8. Unknown -> OFF

        Args:
            state: Pump state dict from cache
        """
        # Priority 3: Fatal error
        if state.get("fatal_error"):
            self._blink_led("red", self.blink_rates["fatal_error"])
            return

        # Priority 4: Stale ADC warning
        adc_stale_start = state.get("adc_stale_started_at")
        if adc_stale_start is not None:
            adc_stale_fatal_sec = state.get("adc_stale_fatal_seconds", 10.0)
            elapsed = time.time() - adc_stale_start
            if 0 < elapsed < adc_stale_fatal_sec:
                # Stale but not fatal yet - show warning
                self._blink_led("red", self.blink_rates["adc_stale"])
                return
            # If elapsed >= fatal threshold, fatal_error should already be True

        # Priority 5-7: Pumping states
        current = state.get("current_state")
        if current == "manual_pumping":
            self._blink_led("green", self.blink_rates["manual_pumping"])
        elif current == "pumping":
            self._blink_led("green", self.blink_rates["pumping"])
        elif current == "not_pumping":
            self.led.set_solid("green")
        else:
            # Unknown state - turn off
            self.led.set_solid("off")

    def _blink_led(self, color: str, rate_hz: float) -> None:
        """Blink LED at specified rate.

        Args:
            color: "red" or "green"
            rate_hz: Blink frequency in Hz (cycles per second)
                    e.g., 2.0 = 2 cycles/sec = 0.5s period (0.25s on, 0.25s off)
        """
        half_period = 1.0 / (rate_hz * 2)
        elapsed = time.monotonic() - self.blink_start_time
        phase = int(elapsed / half_period) % 2

        if phase == 0:
            self.led.set_solid(color)
        else:
            self.led.set_solid("off")


def main() -> None:
    """Main entry point for LED controller service."""
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Load configuration
    try:
        env = load_role("pump_pi")
    except Exception as exc:
        LOGGER.error("Failed to load pump_pi env: %s", exc)
        sys.exit(1)

    # Create service
    service = LEDControllerService(env)

    # Setup signal handlers
    def handle_signal(sig, frame):
        LOGGER.info("Received signal %s, shutting down.", sig)
        service.stop_event.set()
        # CRITICAL: Turn off LED before exit (set both pins LOW)
        service.led.cleanup()
        sys.exit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handle_signal)

    # Run service
    try:
        service.run()
    finally:
        # Ensure cleanup on any exit
        service.led.cleanup()


if __name__ == "__main__":
    main()
