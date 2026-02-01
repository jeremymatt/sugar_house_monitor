# Pump Pi setup

## Overview
The Pump Pi runs the pump control stack (ADC sampling, pump controller, vacuum averaging, uploads, watchdog, and LED status). The pump controller is the only authority that can turn the pump on or off.

## Setup
1) Enable SPI (required for MCP3008):
```bash
sudo raspi-config nonint do_spi 0
```

2) Copy the env template and edit it:
```bash
cp /home/pump/sugar_house_monitor/config/example/pump_pi.env /home/pump/sugar_house_monitor/config/pump_pi.env
```

3) Create the venv and install dependencies:
```bash
/home/pump/sugar_house_monitor/scripts/pump_pi_setup/setup_environment.sh
```
Note: On a Pi Zero, enable system site packages in the venv and use the native numpy package (building wheels can exhaust memory).

4) Install and enable services:
```bash
sudo /home/pump/sugar_house_monitor/scripts/pump_pi_setup/systemd_setup.sh -on
```

## Controls summary
- systemd_setup.sh flags:
  - -on: install/update units and enable auto-restart (production).
  - -off: stop pump stack, keep ADC + watchdog running (hardware re-enable).
  - -full_off: stop all services (maintenance).
- ADC control pins (MCP3008, held high for CONTROL_HOLD_SECONDS):
  - P5: systemd_setup.sh -on
  - P6: systemd_setup.sh -off
  - P7: clear pump controller fatal error state
- Restart after a fatal error:
```bash
sudo systemctl restart sugar-pump-controller.service
```

## Hardware overview
- MCP3008 on SPI0 for all pump inputs.
- Pump relay control pin: GPIO22 (BCM).
- Status LED pins: red GPIO17, green GPIO4 (BCM) using a common-cathode dual-color LED and a current-limiting resistor.

Wiring diagram (BCM numbering):
```
Raspberry Pi                    MCP3008 / Relay / LED
----------------------------------------------------
3.3V -------------------------- VDD, VREF
GND  -------------------------- AGND, DGND, relay GND, LED cathode
GPIO10 (MOSI) ----------------- DIN
GPIO9  (MISO) ----------------- DOUT
GPIO11 (SCLK) ----------------- CLK
GPIO8  (CE0) ------------------ CS
GPIO22 ------------------------ Relay IN (pump control)
GPIO17 ------------------------ Red LED anode (via resistor)
GPIO4  ------------------------ Green LED anode (via resistor)
```

## Additional details

### Safety behavior
- The pump controller is the only authority for pump on/off decisions.
- If the ADC cache goes stale, the controller keeps the pump off until samples resume or an operator intervenes.
- Supervisors restart services only on crash or hang, not on warning/fatal states.

### Service layout
- sugar-adc.service: owns MCP3008 and publishes cached samples (default cache path: /dev/shm/pump_adc_cache.json, override with ADC_CACHE_PATH).
- sugar-pump-controller.service: reads cached samples and drives the relay + state machine.
- sugar-vacuum.service: computes slow vacuum averages from cached samples.
- sugar-uploader.service: uploads pump events, vacuum readings, and error logs.
- sugar-adc-watchdog.service: monitors cached ADC signals and runs systemd_setup.sh -on on a rising service_on edge when the pump stack is off.
- sugar-led-controller.service: visual status indicator using a dual-color (red/green) LED.

### LED status patterns
| LED pattern | System state | Meaning |
|-------------|--------------|---------|
| Red solid | Pump service down | LED controller running, but pump service stopped (normal during -off mode) |
| Red/off/green/off alternating (1 Hz) | Cache stale | LED controller cannot read pump state (communication failure) |
| Red blink 2 Hz | Fatal error | Critical error - pump disabled, requires intervention |
| Red blink 1 Hz | ADC stale warning | ADC communication degraded (2-10 seconds) |
| Green blink 2 Hz | Manual pumping | Manual pump operation active |
| Green blink 1 Hz | Auto pumping | Automatic pump cycle in progress |
| Green solid | Ready (idle) | Normal operation, pump ready |
| Both off | Service off | LED controller service not running |

### LED configuration (pump_pi.env)
```bash
# GPIO Pin Assignments (BCM numbering)
STATUS_LED_RED_PIN=17               # GPIO pin for red LED anode
STATUS_LED_GREEN_PIN=4              # GPIO pin for green LED anode

# LED Blink Rates (Hz = cycles per second)
LED_BLINK_RATE_AUTO=1.0             # Auto pump cycle blink rate
LED_BLINK_RATE_MANUAL=2.0           # Manual pump cycle blink rate
LED_BLINK_RATE_FATAL=2.0            # Fatal error blink rate
LED_BLINK_RATE_STALE=1.0            # ADC stale warning blink rate
LED_ALTERNATING_RATE=1.0            # Cache stale alternating pattern rate

# LED Controller Timing
LED_CACHE_STALE_SECONDS=5.0         # State cache staleness threshold
LED_LOOP_DELAY=0.05                 # LED update loop interval (50ms)
```

### Pump controller state machine (P1/P2/P3 truth table)
- P1 = tank_full input, P2 = manual_start input, P3 = tank_empty input; states are pumping, manual_pumping, and not_pumping.
- error_count increments while an error condition persists and resets when signals clear; if error_count >= error_threshold, the controller forces a fatal stop.

| P1 | P2 | P3 | Error state | Error action | Current state | Next state / action |
|----|----|----|-------------|--------------|---------------|---------------------|
| any | any | any | error_count >= error_threshold | no action | ERROR_STATE | not_pumping / stop loop, flag error on WordPress, set error_message="FATAL ERROR: STOPPING", write/queue error log |
| 0 | 0 | 0 | error_count < error_threshold | reset error_count=0 | pumping | pumping / no action |
| 0 | 0 | 0 | error_count < error_threshold | reset error_count=0 | manual_pumping | manual_pumping / no action |
| 0 | 0 | 0 | error_count < error_threshold | reset error_count=0 | not_pumping | not_pumping / no action |
| 1 | 0 | 0 | error_count < error_threshold | error_count += 1 | pumping | pumping / set error_message="WARNING: received tank full signal while auto pumping" and write/queue error |
| 1 | 0 | 0 | error_count < error_threshold | error_count += 1 | manual_pumping | pumping / set error_message="WARNING: received tank full signal while manual pumping", write/queue error, run tank_full_event_handling() |
| 1 | 0 | 0 | error_count < error_threshold | reset error_count=0 | not_pumping | pumping / run tank_full_event_handling() |
| 0 | 1 | 0 | error_count < error_threshold | reset error_count=0 | pumping | pumping / set error_message="WARNING: received manual pump signal while auto pumping" and write/queue error |
| 0 | 1 | 0 | error_count < error_threshold | reset error_count=0 | manual_pumping | manual_pumping / no action |
| 0 | 1 | 0 | error_count < error_threshold | reset error_count=0 | not_pumping | manual_pumping / record event locally, queue to WordPress, set pump_end_time=None, set pump_start_time=time.time() if missing |
| 0 | 0 | 1 | error_count < error_threshold | reset error_count=0 | pumping | not_pumping / calculate pump time, record locally, queue to WordPress, set pump_end_time=time.time() |
| 0 | 0 | 1 | error_count < error_threshold | reset error_count=0 | manual_pumping | not_pumping / record locally, queue to WordPress, set pump_end_time=time.time() |
| 0 | 0 | 1 | error_count < error_threshold | reset error_count=0 | not_pumping | not_pumping / set pump_end_time=time.time() |
| 1 | 1 | 0 | error_count < error_threshold | error_count += 1 | pumping | pumping / set error_message="WARNING: received simultaneous tank full and manual start signals while auto pumping" and queue to WordPress |
| 1 | 1 | 0 | error_count < error_threshold | error_count += 1 | manual_pumping | pumping / set error_message="WARNING: received simultaneous tank full and manual start signals while manually pumping", queue to WordPress, run tank_full_event_handling() |
| 1 | 1 | 0 | error_count < error_threshold | error_count += 1 | not_pumping | pumping / set error_message="WARNING: received simultaneous tank full and manual start signals while not pumping", queue to WordPress, run tank_full_event_handling() |
| 1 | 0 | 1 | error_count < error_threshold | error_count += 1 | pumping | pumping / set error_message="ERROR: received simultaneous tank empty and tank full signals while auto pumping", write/queue error |
| 1 | 0 | 1 | error_count < error_threshold | error_count += 1 | manual_pumping | manual_pumping / set error_message="ERROR: received simultaneous tank empty and tank full signals while manual pumping", write/queue error |
| 1 | 0 | 1 | error_count < error_threshold | error_count += 1 | not_pumping | pumping / set error_message="ERROR: received simultaneous tank empty and tank full signals while not pumping", write/queue error |
| 0 | 1 | 1 | error_count < error_threshold | reset error_count=0 | pumping | not_pumping / set error_message="WARNING: received simultaneous tank empty and manual pump start signals while auto pumping", write/queue error |
| 0 | 1 | 1 | error_count < error_threshold | reset error_count=0 | manual_pumping | not_pumping / set error_message="WARNING: received simultaneous tank empty and manual pump start signals while manual pumping", write/queue error |
| 0 | 1 | 1 | error_count < error_threshold | reset error_count=0 | not_pumping | not_pumping / set error_message="WARNING: received simultaneous tank empty and manual pump start signals while not pumping", write/queue error |
| 1 | 1 | 1 | error_count < error_threshold | error_count += 1 | pumping | pumping / set error_message="ERROR: received simultaneous tank empty, manual start, and tank full signals while auto pumping", write/queue error |
| 1 | 1 | 1 | error_count < error_threshold | error_count += 1 | manual_pumping | manual_pumping / set error_message="ERROR: received simultaneous tank empty, manual start, and tank full signals while manual pumping", write/queue error |
| 1 | 1 | 1 | error_count < error_threshold | error_count += 1 | not_pumping | pumping / set error_message="ERROR: received simultaneous tank empty, manual start, and tank full signals while not pumping", write/queue error |

- tank_full_event_handling(): sets pump_start_time if missing; when pump_end_time is present, computes fill_time and flow_rate, records an event, and clears pump_end_time; otherwise logs a warning that pump_end_time was missing.

### Unit templates
Unit templates live in scripts/pump_pi_setup/systemd/. The systemd_setup.sh script installs them into /etc/systemd/system and fills in the repo path, user, venv path, and log location.

### Logs
```bash
tail -f ~/pump_controller.log
journalctl -u sugar-pump-controller.service -f
```
Log rotation is installed by systemd_setup.sh -on at /etc/logrotate.d/sugar-pump (2MB, keep 5).

### Optional hard-stop on service exit
If you want an explicit GPIO off on service stop, add this to the pump controller unit:
```ini
ExecStopPost=/usr/bin/raspi-gpio set 22 op dl
```

## Error info
- ADC stale detection: ADC_STALE_SECONDS triggers warnings; ADC_STALE_FATAL_SECONDS forces a fatal stop.
- Safety checks increment error_count on invalid signal combinations; once error_count >= ERROR_THRESHOLD the controller transitions to a fatal stop state.
- Errors are logged locally and uploaded to the server by the uploader service.
- Supervisors restart on crash/hang; fatal states require manual clear (P7 or service restart).
