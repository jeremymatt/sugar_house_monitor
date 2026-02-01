# O2 Pi setup

## Overview
The O2 Pi samples an oxygen sensor through an MCP3008 ADC (SPI) and uploads readings to the server. A status LED shows normal vs error states.

## Setup
1) Enable SPI (required for MCP3008):
```bash
sudo raspi-config nonint do_spi 0
```

2) Copy the env template and edit it:
```bash
cp /home/pi/sugar_house_monitor/config/example/oh_two_pi.env /home/pi/sugar_house_monitor/config/oh_two_pi.env
```

3) Create the venv and install dependencies:
```bash
/home/pi/sugar_house_monitor/scripts/oh_two_pi_setup/setup_environment.sh
```

4) Install and enable the service:
```bash
sudo /home/pi/sugar_house_monitor/scripts/oh_two_pi_setup/systemd_setup.sh -on
```

## Controls summary
- systemd_setup.sh flags:
  - -on: install/update units and enable auto-restart (production).
  - -off: stop service and disable auto-restart (testing).
- Status LED behavior is described in Additional details below.

## Hardware overview
- MCP3008 on SPI0, CS on GPIO5 (BCM).
- O2 sensor analog output into MCP3008 channel P0.
- Status LED on GPIO23 (BCM).

Wiring diagram (BCM numbering):
```
Raspberry Pi                    MCP3008 / Sensor / LED
------------------------------------------------------
3.3V -------------------------- VDD, VREF
GND  -------------------------- AGND, DGND, sensor GND, LED cathode
GPIO10 (MOSI) ----------------- DIN
GPIO9  (MISO) ----------------- DOUT
GPIO11 (SCLK) ----------------- CLK
GPIO5  (CE1) ------------------ CS
MCP3008 P0 -------------------- O2 sensor analog out
GPIO23 ------------------------ Status LED anode (via resistor)
```

## Additional details
- Calibration is read from scripts/oh_two_cal.csv (update it with your sensor calibration data).
- The service uploads to ingest_oh_two.php and keeps a local SQLite queue for retries.
- Status LED:
  - Solid on: normal operation.
  - Blinking 1 Hz: error detected in sampling or upload.

## Error info
- Sampling failures trigger LED blinking and a retry loop; the service attempts to continue sampling.
- Upload failures are retried on the next upload interval; backlog size is logged.
- Systemd restarts the service on crash; check the journal for repeated failures.
