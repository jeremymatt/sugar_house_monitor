# Pump Pi System Documentation

## 1. Overview

The Pump Pi controls a transfer pump for maple sap using a safety-focused state machine with hardware interlocks. The system runs as multiple microservices supervised by systemd, with each service handling a specific role:

- **ADC Service** (`sugar-adc`): Reads MCP3008 ADC channels and caches digital/analog signals
- **Pump Controller** (`sugar-pump-controller`): State machine logic, relay control, safety interlocks
- **Vacuum Service** (`sugar-vacuum`): Computes slow vacuum averages from ADC samples
- **Upload Service** (`sugar-uploader`): Uploads pump events, vacuum readings, and error logs to server
- **LED Controller** (`sugar-led-controller`): Visual status indicator with red/green dual-color LED
- **ADC Watchdog** (`sugar-adc-watchdog`): Monitors ADC signals and auto-restarts pump services on hardware trigger

**Key Design Principles**:
- **Safety-first**: Pump controller is the sole authority for relay on/off decisions
- **Stale data protection**: Incoherent or stale sensor data forces pump off; escalates to fatal after 10 seconds
- **Service isolation**: Each process can be restarted independently; failures don't cascade
- **Shared memory IPC**: Services communicate via `/dev/shm/*.json` cache files

## 2. Setup Instructions

### Prerequisites

1. **Enable SPI interface**:
   ```bash
   sudo raspi-config nonint do_spi 0
   ```

2. **Hardware wiring** (see [Hardware Overview](#4-hardware-overview-wiring) below)

### Environment Configuration

1. Copy the example config and customize:
   ```bash
   cp /home/pump/sugar_house_monitor/config/example/pump_pi.env \
      /home/pump/sugar_house_monitor/config/pump_pi.env
   nano /home/pump/sugar_house_monitor/config/pump_pi.env
   ```

2. Key settings to configure:
   - `API_BASE_URL`: Your WordPress server endpoint
   - `API_KEY`: Shared authentication key (generate with `gen_credentials.py`)
   - `PUMP_CONTROL_PIN`: GPIO pin for relay (default: GPIO22)
   - `STATUS_LED_RED_PIN`, `STATUS_LED_GREEN_PIN`: LED GPIO pins (defaults: GPIO17, GPIO4)
   - `ADC_STALE_SECONDS`, `ADC_STALE_FATAL_SECONDS`: Safety timeout thresholds

### Virtual Environment Setup

Run the automated setup script:
```bash
/home/pump/sugar_house_monitor/scripts/pump_pi_setup/setup_environment.sh
```

This creates a Python virtual environment at `~/.venv` and installs all dependencies.

### Systemd Service Installation

Install and enable all pump services:
```bash
sudo /home/pump/sugar_house_monitor/scripts/pump_pi_setup/systemd_setup.sh -on
```

This installs unit files from `systemd/` directory to `/etc/systemd/system/`, enables auto-restart, and starts all services.

## 3. Controls Summary

### Quick Start Commands

**Production mode** (enable auto-restart, pump operational):
```bash
sudo /home/pump/sugar_house_monitor/scripts/pump_pi_setup/systemd_setup.sh -on
```
LED shows green (solid or blinking) during normal operation.

**Watchdog mode** (stop pump stack, keep ADC + watchdog running for hardware re-enable):
```bash
sudo /home/pump/sugar_house_monitor/scripts/pump_pi_setup/systemd_setup.sh -off
```
LED shows **red solid** (pump service intentionally stopped, LED controller continues monitoring).

**Maintenance mode** (stop everything, including ADC + watchdog):
```bash
sudo /home/pump/sugar_house_monitor/scripts/pump_pi_setup/systemd_setup.sh -full_off
```
LED turns **OFF** (complete shutdown). Use before editing `adc_service.py` or MCP3008 wiring.

### Manual Service Control

**Restart pump controller** (e.g., to clear fatal error):
```bash
sudo systemctl restart sugar-pump-controller.service
```

**Check service status**:
```bash
systemctl status sugar-pump-controller
systemctl status sugar-adc
systemctl status sugar-led-controller
```

**View logs**:
```bash
tail -f ~/pump_controller.log
journalctl -u sugar-pump-controller -f
journalctl -u sugar-led-controller -f
```

### ADC Control Pins (Hardware Triggers)

The following MCP3008 channels trigger service actions when held HIGH for ≥5 seconds (configurable via `CONTROL_HOLD_SECONDS`):

- **P5**: `systemd_setup.sh -on` (enable/start pump services)
- **P6**: `systemd_setup.sh -off` (disable/stop pump services)
- **P7**: Clear pump controller fatal error state

Threshold voltage: `ADC_BOOL_THRESHOLD_V` (default 1.0V).

### Key Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `PUMP_CONTROL_PIN` | 22 | GPIO pin (BCM) for pump relay control |
| `ADC_REFERENCE_VOLTAGE` | 5 | MCP3008 reference voltage |
| `ADC_BOOL_THRESHOLD_V` | 1.0 | Digital signal threshold (volts) |
| `ADC_STALE_SECONDS` | 2 | ADC warning threshold (seconds) |
| `ADC_STALE_FATAL_SECONDS` | 10 | Force pump off after ADC stale (seconds) |
| `ERROR_THRESHOLD` | 30 | Error count before fatal stop |
| `LOOP_DELAY` | 0.1 | Pump controller loop interval (seconds) |
| `STATUS_LED_RED_PIN` | 17 | GPIO pin for red LED anode |
| `STATUS_LED_GREEN_PIN` | 4 | GPIO pin for green LED anode |
| `LED_BLINK_RATE_AUTO` | 1.0 | Auto pump blink rate (Hz) |
| `LED_BLINK_RATE_MANUAL` | 2.0 | Manual pump blink rate (Hz) |
| `LED_BLINK_RATE_FATAL` | 2.0 | Fatal error blink rate (Hz) |
| `LED_BLINK_RATE_STALE` | 1.0 | ADC stale warning blink rate (Hz) |
| `VERBOSE` | false | Force periodic signal summaries to logs |

Full config reference: `config/example/pump_pi.env`

## 4. Hardware Overview & Wiring

### GPIO Pin Assignments (BCM Numbering)

| Function | GPIO Pin | Direction | Notes |
|----------|----------|-----------|-------|
| **Pump Relay Control** | GPIO22 | Output | Active HIGH to energize relay |
| **LED Red Anode** | GPIO17 | Output | Error/warning states |
| **LED Green Anode** | GPIO4 | Output | Normal operation states |

### MCP3008 ADC (SPI)

**SPI Interface**:
- MOSI: GPIO10 (SPI0_MOSI)
- MISO: GPIO9 (SPI0_MISO)
- SCLK: GPIO11 (SPI0_SCLK)
- CS0: GPIO8 (SPI0_CE0_N)

**ADC Channels**:
- **P0**: Tank full signal (digital, threshold: 1.0V)
- **P1**: Manual start signal (digital)
- **P2**: Tank empty signal (digital)
- **P3**: Vacuum sensor (analog, 0-5V)
- **P4**: (Reserved)
- **P5**: Service ON control (digital, hold 5s)
- **P6**: Service OFF control (digital, hold 5s)
- **P7**: Clear fatal error (digital, hold 5s)

**Reference voltage**: 5V (configurable via `ADC_REFERENCE_VOLTAGE`)

### Status LED Wiring

**Component**: Common-cathode dual-color (red/green) LED

**CRITICAL SAFETY CONSTRAINT**: Only ONE color can be active at a time due to shared cathode with single current-limiting resistor. Software enforces mutual exclusivity.

**Wiring Diagram**:
```
                 150Ω resistor
                      │
    GPIO17 (Red) ─────┤>├──┐
                 Red LED    │
                            ├─── GND (Common Cathode)
    GPIO4 (Green) ────┤>├──┘
                Green LED
```

**Component selection**:
- Typical forward voltage (Vf): 2.0V
- Typical forward current (If): 10mA
- Resistor calculation: R = (3.3V - Vf) / If = (3.3 - 2.0) / 0.01 = 130Ω → use 150Ω standard value
- **Do not exceed 16mA** per GPIO pin (Raspberry Pi limit)

**LED State Patterns**:

| LED Pattern | System State | Meaning |
|-------------|--------------|---------|
| **Red solid** | Pump service down | LED controller running, pump service stopped (normal during `-off` mode) |
| **Red/off/green/off alternating (1Hz)** | Cache stale | LED controller cannot read pump state (communication failure) |
| **Red blink 2Hz** | Fatal error | Critical error - pump disabled, requires intervention |
| **Red blink 1Hz** | ADC stale warning | ADC communication degraded (2-10 seconds) |
| **Green blink 2Hz** | Manual pumping | Manual pump operation active |
| **Green blink 1Hz** | Auto pumping | Automatic pump cycle in progress |
| **Green solid** | Ready (idle) | Normal operation, pump ready |
| **Both OFF** | Service off | LED controller service not running |

### Pump Relay Wiring

**GPIO22** controls a relay module (active HIGH). Relay switches 120VAC pump power.

**Relay Module**:
- VCC: 5V (from Pi)
- GND: GND
- IN: GPIO22
- NO (Normally Open): To pump hot wire
- COM (Common): From 120VAC hot

**Safety**: Relay module provides optical isolation between Pi logic and AC power.

## 5. Additional Details

### Safety and State Machine Behavior

**Core safety principles**:
- Pump controller remains the **only authority** for pump on/off decisions
- Incoherent or stale sensor data forces pump off immediately
- After `ADC_STALE_FATAL_SECONDS` (default 10s), controller enters fatal error state and stops
- Supervisors restart only on crash or hang, **not** on "fatal" or "warning" states
- If ADC service stops updating, pump controller treats samples as stale and keeps pump off until samples resume

**State machine**:
- **States**: `not_pumping`, `pumping`, `manual_pumping`
- **Inputs**: P0 (tank_full), P1 (manual_start), P2 (tank_empty)
- **Safety**: Error count increments during invalid conditions; ≥30 errors triggers fatal stop
- **Relay logic**: Pump ON only if `(pumping OR manual_pumping) AND NOT fatal_error`

For complete state machine truth table (16 input combinations), see original main README.md or `main_pump.py` documentation.

### Recommended Supervision Model

Systemd supervises each process directly. A custom supervisor script is **not recommended** because it creates a single point of failure (all children die if supervisor crashes).

**Service dependencies**:
- `sugar-pump-controller.service` has `After=sugar-adc.service` (waits for ADC to start)
- `sugar-vacuum.service` depends on ADC cache
- `sugar-uploader.service` runs independently
- `sugar-led-controller.service` has `Wants=sugar-pump-controller.service` (preferred but not required)

**Auto-restart**: All services configured with `Restart=always` and `RestartSec=2` (or 5s for LED).

### Service Layout

| Service | Script | Purpose | Cache |
|---------|--------|---------|-------|
| `sugar-adc.service` | `adc_service.py` | Owns MCP3008, publishes samples | `/dev/shm/pump_adc_cache.json` |
| `sugar-pump-controller.service` | `pump_controller.py` | State machine + relay driver | Reads ADC cache, writes `/dev/shm/pump_state_cache.json` |
| `sugar-vacuum.service` | `vacuum_service.py` | Slow vacuum averaging | Reads ADC cache |
| `sugar-uploader.service` | `upload_service.py` | Uploads events/logs to server | Reads local SQLite DB |
| `sugar-adc-watchdog.service` | `adc_watchdog.py` | Hardware re-enable via ADC pins | Monitors ADC cache, runs `systemd_setup.sh` |
| `sugar-led-controller.service` | `led_controller.py` | Visual status indicator | Reads pump state cache |

**Target**: `sugar-pump.target` groups pump-controller, vacuum, and uploader for coordinated start/stop.

### Unit File Templates

Unit templates live in `scripts/pump_pi_setup/systemd/`. The `systemd_setup.sh` script:
1. Renders templates with actual paths (repo root, venv, user, log location)
2. Installs to `/etc/systemd/system/`
3. Runs `daemon-reload`
4. Enables/disables services based on flag (`-on`, `-off`, `-full_off`)

**Example unit**:
```ini
[Unit]
Description=Sugar House Pump Controller
After=sugar-adc.service

[Service]
Type=simple
User=pump
WorkingDirectory=/home/pump/sugar_house_monitor
Environment=PYTHONUNBUFFERED=1
Environment=SUGAR_CONFIG_DIR=/home/pump/sugar_house_monitor/config
ExecStart=/home/pump/.venv/bin/python /home/pump/sugar_house_monitor/scripts/pump_controller.py
Restart=always
RestartSec=2
StandardOutput=append:/home/pump/pump_controller.log
StandardError=append:/home/pump/pump_controller.log

[Install]
WantedBy=multi-user.target
```

### Log Management

**Service logs**:
```bash
tail -f ~/pump_controller.log              # Direct log file
journalctl -u sugar-pump-controller -f      # Systemd journal
journalctl -u sugar-adc -f                  # ADC service logs
```

**Log rotation**: Installed by `systemd_setup.sh -on` at `/etc/logrotate.d/sugar-pump`:
- Rotate at 2MB
- Keep 5 old logs
- Compress with gzip

**Debug logging**: Set `VERBOSE=true` in `pump_pi.env` to force periodic signal summaries even when all inputs are low.

### Optional Hard-Stop on Service Exit

If you want an explicit GPIO off when pump service stops, add to `sugar-pump-controller.service`:
```ini
ExecStopPost=/usr/bin/raspi-gpio set 22 op dl
```

This ensures GPIO22 (relay) is set to OUTPUT LOW when service exits.

## 6. Error Information & Troubleshooting

### LED Pattern Diagnostics

**Red solid** - Normal when pump stack is off (watchdog mode):
- **If unexpected**: Check pump service status:
  ```bash
  systemctl status sugar-pump-controller
  ```
- **To start pump**:
  ```bash
  sudo systemd_setup.sh -on
  ```

**Alternating red/green** - Communication failure between pump and LED controller:
- **Check pump state cache**:
  ```bash
  cat /dev/shm/pump_state_cache.json
  ```
- **Restart pump controller**:
  ```bash
  sudo systemctl restart sugar-pump-controller
  ```

**Red blink 2Hz** - Fatal error state:
- **Check logs**:
  ```bash
  journalctl -u sugar-pump-controller -n 50
  ```
- **Common causes**: ADC stale >10s, critical safety condition
- **Clear fatal**: Via P7 ADC signal (hold HIGH for 5s), or restart:
  ```bash
  sudo systemctl restart sugar-pump-controller
  ```

**Red blink 1Hz** - ADC stale warning (early detection):
- **Check ADC service**:
  ```bash
  systemctl status sugar-adc
  ```
- **Check ADC cache**:
  ```bash
  cat /dev/shm/pump_adc_cache.json
  ```
- **If persistent**: Restart ADC service:
  ```bash
  sudo systemctl restart sugar-adc
  ```

**Green patterns** - Normal operation:
- Solid green: Pump ready, no active pumping
- Blink 1Hz: Auto pumping cycle
- Blink 2Hz: Manual pumping (faster blink)

### Common Errors

**"This channel is already in use" (GPIO warning)**:
- **Cause**: GPIO pins configured from previous run/crash
- **Solution**: GPIO cleanup is automatic in `status_led.py` and `main_pump.py`
- **If persistent**: Manually cleanup:
  ```bash
  sudo systemctl stop sugar-pump-controller sugar-led-controller
  # Wait 5 seconds for cleanup
  sudo systemctl start sugar-pump-controller sugar-led-controller
  ```

**"Permission denied" on SPI**:
- **Cause**: User not in `spi` group
- **Solution**:
  ```bash
  sudo usermod -a -G spi pump
  # Log out and back in
  ```

**Pump won't start despite green LED**:
- Check relay wiring (GPIO22 → relay IN)
- Verify relay module power (5V, GND)
- Test GPIO output manually:
  ```bash
  raspi-gpio set 22 op dh  # Set HIGH
  raspi-gpio get 22        # Verify state
  raspi-gpio set 22 op dl  # Set LOW
  ```

**ADC reads all zeros**:
- Check SPI wiring (MOSI, MISO, SCLK, CS0)
- Verify MCP3008 power (3.3V or 5V depending on model)
- Test SPI interface:
  ```bash
  ls -l /dev/spidev0.0  # Should exist if SPI enabled
  ```

**Services fail to start after reboot**:
- Check venv path: `ls /home/pump/.venv/bin/python3`
- Verify config exists: `ls /home/pump/sugar_house_monitor/config/pump_pi.env`
- Check service unit files: `systemctl cat sugar-pump-controller`

### Getting Help

**Collect diagnostic info**:
```bash
# Service status
systemctl status sugar-pump-controller sugar-adc sugar-led-controller

# Recent logs
journalctl -u sugar-pump-controller -n 100 > pump_logs.txt

# Cache contents
cat /dev/shm/pump_adc_cache.json
cat /dev/shm/pump_state_cache.json

# GPIO states
gpio readall

# Config (sanitize API_KEY before sharing!)
cat /home/pump/sugar_house_monitor/config/pump_pi.env
```

Report issues at: https://github.com/jeremymatt/sugar_house_monitor/issues
