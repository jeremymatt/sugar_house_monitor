#!/usr/bin/env python3
"""Status LED controller for dual-color (red/green) common-cathode LED.

This module provides a StatusLED class for controlling a common-cathode dual-color LED
with mutual exclusivity enforcement (only one color can be active at a time).
"""
import logging

LOGGER = logging.getLogger(__name__)


class StatusLED:
    """Controls a common-cathode dual-color (red/green) LED via GPIO pins.

    Hardware constraint: Only ONE LED can be active at a time due to shared cathode
    with single current-limiting resistor. This class enforces mutual exclusivity
    in software to prevent component damage.

    Attributes:
        red_pin: BCM GPIO pin number for red LED anode
        green_pin: BCM GPIO pin number for green LED anode
        available: True if GPIO is available, False otherwise
        GPIO: RPi.GPIO module reference (None if unavailable)
    """

    def __init__(self, red_pin: int, green_pin: int):
        """Initialize StatusLED with GPIO pins.

        Args:
            red_pin: BCM GPIO pin number for red LED anode
            green_pin: BCM GPIO pin number for green LED anode
        """
        self.red_pin = red_pin
        self.green_pin = green_pin
        self.available = False
        self.GPIO = None

        try:
            import RPi.GPIO as GPIO
            self.GPIO = GPIO

            # Set BCM numbering mode
            GPIO.setmode(GPIO.BCM)

            # CRITICAL SAFETY: Set initial=GPIO.LOW for BOTH pins
            # Prevents LED damage if both accidentally set HIGH during startup
            GPIO.setup(red_pin, GPIO.OUT, initial=GPIO.LOW)
            GPIO.setup(green_pin, GPIO.OUT, initial=GPIO.LOW)

            self.available = True
            LOGGER.info("StatusLED initialized: red=GPIO%d, green=GPIO%d", red_pin, green_pin)

        except Exception as exc:
            LOGGER.warning("GPIO not available for StatusLED: %s", exc)
            self.GPIO = None
            self.available = False

    def set_solid(self, color: str) -> None:
        """Set LED to a solid color with mutual exclusivity enforcement.

        Args:
            color: One of "red", "green", or "off"

        Note:
            This method enforces mutual exclusivity - only one LED can be HIGH at a time.
            If an invalid color is provided, both LEDs are turned OFF (fail-safe).
        """
        if not self.available or self.GPIO is None:
            return

        try:
            if color == "red":
                # Turn off green, then turn on red
                self.GPIO.output(self.green_pin, self.GPIO.LOW)
                self.GPIO.output(self.red_pin, self.GPIO.HIGH)

            elif color == "green":
                # Turn off red, then turn on green
                self.GPIO.output(self.red_pin, self.GPIO.LOW)
                self.GPIO.output(self.green_pin, self.GPIO.HIGH)

            elif color == "off":
                # Turn off both LEDs
                self.GPIO.output(self.red_pin, self.GPIO.LOW)
                self.GPIO.output(self.green_pin, self.GPIO.LOW)

            else:
                # Invalid color - fail safe to OFF
                LOGGER.error("Invalid LED color: %s (expected 'red', 'green', or 'off')", color)
                self.GPIO.output(self.red_pin, self.GPIO.LOW)
                self.GPIO.output(self.green_pin, self.GPIO.LOW)

        except Exception as exc:
            LOGGER.error("Failed to set LED color %s: %s", color, exc, exc_info=True)

    def cleanup(self) -> None:
        """Turn off LED and cleanup GPIO pins before exit.

        This method should be called before the program exits to ensure:
        1. Both LEDs are turned OFF (both pins LOW)
        2. GPIO pins are released for other programs
        """
        if not self.available or self.GPIO is None:
            return

        try:
            # CRITICAL: Set both pins LOW before cleanup (turn off LED)
            self.GPIO.output(self.red_pin, self.GPIO.LOW)
            self.GPIO.output(self.green_pin, self.GPIO.LOW)

            # Release GPIO pins
            self.GPIO.cleanup(self.red_pin)
            self.GPIO.cleanup(self.green_pin)

            LOGGER.info("StatusLED cleanup complete")

        except Exception as exc:
            LOGGER.warning("StatusLED cleanup error: %s", exc)
