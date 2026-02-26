#!/usr/bin/env python3
"""
Simple Tank Pi sensor test script.

Reads raw distance from both Brookside and Roadside ultrasonic sensors
once per second and displays results on the console and 2x16 LCD.
"""
import signal
import sys
import time

import serial
import board
import busio
import adafruit_character_lcd.character_lcd_rgb_i2c as character_lcd

SERIAL_PORTS = {
    "brookside": "/dev/serial0",
    "roadside": "/dev/ttyAMA5",
}
BAUD_RATE = 9600
UART_TIMEOUT = 0.5
TRIGGER_BYTE = 0x55


def open_uart(port):
    try:
        return serial.Serial(port, baudrate=BAUD_RATE, timeout=UART_TIMEOUT)
    except Exception as exc:
        print(f"  WARNING: Could not open {port}: {exc}")
        return None


def read_distance_inches(uart):
    """Read one distance measurement. Returns inches or None on failure."""
    if uart is None:
        return None
    uart.reset_input_buffer()
    uart.write(bytes([TRIGGER_BYTE]))
    time.sleep(0.1)
    if uart.in_waiting <= 0:
        return None
    time.sleep(0.004)
    if uart.read(1) != b"\xff":
        return None
    buf = uart.read(3)
    if len(buf) != 3:
        return None
    checksum = (0xFF + buf[0] + buf[1]) & 0xFF
    if buf[2] != checksum:
        return None
    distance_mm = (buf[0] << 8) + buf[1]
    return round(distance_mm / 25.4, 1)


def main():
    print("=== Tank Pi Sensor Test ===\n")

    # Open UART connections
    print("Opening serial ports...")
    uarts = {}
    for name, port in SERIAL_PORTS.items():
        print(f"  {name}: {port}", end=" ")
        uarts[name] = open_uart(port)
        if uarts[name]:
            print("OK")
        # else warning already printed by open_uart

    # Initialize LCD
    print("\nInitializing LCD...")
    lcd = None
    try:
        i2c = busio.I2C(board.SCL, board.SDA)
        lcd = character_lcd.Character_LCD_RGB_I2C(i2c, 16, 2)
        lcd.color = [100, 0, 0]
        lcd.clear()
        lcd.message = "Sensor Test..."
        print("  LCD OK")
    except Exception as exc:
        print(f"  WARNING: LCD init failed: {exc}")
        print("  (continuing without LCD)")

    # Graceful shutdown
    def shutdown(sig, frame):
        print("\nShutting down...")
        if lcd:
            lcd.clear()
            lcd.color = [0, 0, 0]
        for u in uarts.values():
            if u:
                u.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    print("\nReading sensors (Ctrl+C to stop):\n")
    time.sleep(1)

    while True:
        bs = read_distance_inches(uarts["brookside"])
        rs = read_distance_inches(uarts["roadside"])

        bs_str = f"{bs:.1f}" if bs is not None else "---"
        rs_str = f"{rs:.1f}" if rs is not None else "---"

        print(f"BS: {bs_str} in  |  RS: {rs_str} in")

        if lcd:
            lcd.clear()
            lcd.message = f"BS: {bs_str} in\nRS: {rs_str} in"

        time.sleep(1)


if __name__ == "__main__":
    main()
