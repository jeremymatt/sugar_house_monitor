# Tank Pi setup

## Overview
The Tank Pi reads two UART ultrasonic sensors (Brookside and Roadside), computes volume/flow/ETA locally, and uploads readings/events to the server. It also hosts an optional local fallback UI from web/.

## Setup
1) Enable hardware serial and I2C:
```bash
sudo raspi-config nonint do_serial_cons 1
sudo raspi-config nonint do_serial_hw 0
sudo raspi-config nonint do_i2c 0
```

2) Enable a second UART (uart5). On Bookworm use /boot/firmware/config.txt (older OS uses /boot/config.txt):
```bash
sudo vi /boot/firmware/config.txt
```
Ensure one dtoverlay entry is enabled:
```
enable_uart=1
# dtoverlay=uart2   # TX: GPIO0 /  RX: GPIO1
# dtoverlay=uart3   # TX: GPIO4 /  RX: GPIO5
# dtoverlay=uart4   # TX: CE01    /  RX: MISO
dtoverlay=uart5   # TX: GPIO12 / RX: GPIO13
```

3) Copy the env template and edit it:
```bash
cp /home/pump/sugar_house_monitor/config/example/tank_pi.env /home/pump/sugar_house_monitor/config/tank_pi.env
```

4) Create the venv and install dependencies:
```bash
/home/pump/sugar_house_monitor/scripts/tank_pi_setup/setup_environment.sh
```

5) Install and enable the service:
```bash
sudo /home/pump/sugar_house_monitor/scripts/tank_pi_setup/systemd_setup.sh -on
```

## Controls summary
- systemd_setup.sh flags:
  - -on: install/update units and enable auto-restart (production).
  - -off: stop service and disable auto-restart (testing).
- Manual run in testing mode:
```bash
sudo /home/pump/sugar_house_monitor/scripts/tank_pi_setup/systemd_setup.sh -off
python /home/pump/sugar_house_monitor/scripts/main_tank.py
```
- Debug controls live in config/tank_pi.env (DEBUG_TANK, DEBUG_RELEASER, DEBUG_LOOP_DATA, SYNTHETIC_CLOCK_MULTIPLIER).

## Hardware overview
- Two UART ultrasonic sensors (Brookside/Roadside).
- Optional I2C LCD on SDA/SCL.
- UART ports:
  - Primary UART: GPIO14 (TX) / GPIO15 (RX)
  - Second UART (uart5): GPIO12 (TX) / GPIO13 (RX)

Wiring diagram (BCM numbering):
```
Raspberry Pi                 Sensors / LCD
------------------------------------------------
3.3V ----------------------- Sensor VCC (per module spec)
GND  ----------------------- Sensor GND, LCD GND
GPIO14 (TXD) --------------- UART sensor #1 RX
GPIO15 (RXD) --------------- UART sensor #1 TX
GPIO12 (TXD5) -------------- UART sensor #2 RX
GPIO13 (RXD5) -------------- UART sensor #2 TX
GPIO2  (SDA) --------------- I2C LCD SDA (optional)
GPIO3  (SCL) --------------- I2C LCD SCL (optional)
```

## Additional details
- Local fallback UI: set LOCAL_HTTP_PORT and WEB_ROOT in config/tank_pi.env to serve web/ locally while replaying/debugging.
- Debug replay: set DEBUG_TANK and point BROOKSIDE_CSV/ROADSIDE_CSV/PUMP_EVENTS_CSV at real_data/ to simulate sensor feeds.
- Debug timing log: set DEBUG_SAMPLE_PROCESS_TIMING=true to write data/sample_process_time.csv.
- Unit template lives in scripts/tank_pi_setup/systemd/sugar-tank.service.
- Logs:
```bash
tail -f ~/tank_controller.log
journalctl -u sugar-tank.service -f
```

## Error info
- Upload failures are retried; HTTP errors are logged with response details when available.
- Error events are written to the local error_logs table and web/tank_error_log.txt, then uploaded to ingest_error.php when the network is available.
- Systemd restarts the service on crash; check the journal for repeated failures.
