# Pump Pi systemd setup

This folder documents a multi-process layout so SPI access is centralized and each role can be restarted independently.

## Safety and state machine behavior
- The pump controller remains the only authority for pump on/off decisions.
- Incoherent or stale sensor data is handled inside the pump controller: it forces the pump off and escalates to fatal after `ADC_STALE_FATAL_SECONDS`.
- Supervisors restart only on crash or hang, not on "fatal" or "warning" states.
- If the ADC service stops updating, the pump controller treats samples as stale and keeps the pump off until samples resume or an operator intervenes.

## Recommended supervision model
Systemd supervises each process directly. A custom supervisor script is optional, but it adds a single point of failure because all children die if it crashes.

## Service layout
- `sugar-adc.service`: owns MCP3008 and publishes cached samples (default cache path: `/dev/shm/pump_adc_cache.json`, override with `ADC_CACHE_PATH`).
- `sugar-pump-controller.service`: reads cached samples and drives the relay + state machine.
- `sugar-vacuum.service`: computes slow vacuum averages from cached samples.
- `sugar-uploader.service`: uploads pump events, vacuum readings, and error logs.
- `sugar-adc-watchdog.service`: monitors cached ADC signals and runs `systemd_setup.sh -on` on a rising service_on edge when the pump stack is off.
- `sugar-led-controller.service`: visual status indicator using a dual-color (red/green) LED.

## LED Status Indicator

The LED controller provides real-time visual feedback about the pump system status using a common-cathode dual-color LED.

### Hardware Setup
- **Red LED anode**: GPIO21 (BCM)
- **Green LED anode**: GPIO20 (BCM)
- **Common cathode**: Connect to GND via current-limiting resistor (e.g., 150Ω for typical 2V LEDs)
- **CRITICAL**: Only one color can be active at a time (shared cathode with single resistor)

### LED State Patterns

| LED Pattern | System State | Meaning |
|-------------|--------------|---------|
| **Red solid** | Pump service down | LED controller running, but pump service stopped (normal during `-off` mode) |
| **Red/off/green/off alternating (1Hz)** | Cache stale | LED controller cannot read pump state (communication failure) |
| **Red blink 2Hz** | Fatal error | Critical error - pump disabled, requires intervention |
| **Red blink 1Hz** | ADC stale warning | ADC communication degraded (2-10 seconds) |
| **Green blink 2Hz** | Manual pumping | Manual pump operation active |
| **Green blink 1Hz** | Auto pumping | Automatic pump cycle in progress |
| **Green solid** | Ready (idle) | Normal operation, pump ready |
| **Both OFF** | Service off | LED controller service not running |

### Troubleshooting LED Patterns

**Red solid** - Normal when pump stack is off (watchdog mode)
- If unexpected: Check pump service status: `systemctl status sugar-pump-controller`
- To start pump: `sudo systemd_setup.sh -on`

**Alternating red/green** - Communication failure between pump and LED controller
- Check pump state cache: `cat /dev/shm/pump_state_cache.json`
- Restart pump controller: `sudo systemctl restart sugar-pump-controller`

**Red blink 2Hz** - Fatal error state
- Check pump logs: `journalctl -u sugar-pump-controller -n 50`
- Common causes: ADC stale >10s, critical safety condition
- Clear fatal via P7 signal or restart: `sudo systemctl restart sugar-pump-controller`

**Red blink 1Hz** - ADC stale warning (early detection)
- Check ADC service: `systemctl status sugar-adc`
- Check ADC cache: `cat /dev/shm/pump_adc_cache.json`
- If persistent: Restart ADC service

**Green patterns** - Normal operation
- Solid green: Pump ready, no active pumping
- Blink 1Hz: Auto pumping cycle
- Blink 2Hz: Manual pumping (faster blink)

### LED Configuration (pump_pi.env)

```bash
# GPIO Pin Assignments (BCM numbering)
STATUS_LED_RED_PIN=21               # GPIO pin for red LED anode
STATUS_LED_GREEN_PIN=20             # GPIO pin for green LED anode

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

### LED Controller Service Commands

Check status:
```bash
systemctl status sugar-led-controller
```

View logs:
```bash
journalctl -u sugar-led-controller -f
```

Restart (LED turns off cleanly, then restarts):
```bash
sudo systemctl restart sugar-led-controller
```

Note: The LED controller is **independent** from the pump controller. LED failures do not affect pump operation.

## Quick start
Production mode (enable auto-restart):
```bash
sudo /home/pump/sugar_house_monitor/scripts/pump_pi_setup/systemd_setup.sh -on
```
**LED**: Shows green (solid or blinking) during normal operation

Watchdog mode (stop pump stack, keep ADC + watchdog running for hardware re-enable):
```bash
sudo /home/pump/sugar_house_monitor/scripts/pump_pi_setup/systemd_setup.sh -off
```
**LED**: Shows red solid (pump service intentionally stopped, LED controller continues running)

Maintenance mode (stop everything, including ADC + watchdog):
```bash
sudo /home/pump/sugar_house_monitor/scripts/pump_pi_setup/systemd_setup.sh -full_off
```
**LED**: Turns OFF (complete shutdown)

Use `-full_off` before editing `scripts/adc_service.py` or MCP3008 wiring so SPI access is fully stopped.

If you want to run the monolith manually, use `-full_off` first:
```bash
python /home/pump/sugar_house_monitor/scripts/main_pump.py
```

To restart the pump controller after entering fatal error state:
```bash
sudo systemctl restart sugar-pump-controller.service
```
You can also clear the fatal state via ADC channel P7 (see "ADC control pins" below).

## ADC control pins
These MCP3008 channels trigger service actions after they stay high for at least `CONTROL_HOLD_SECONDS` (default 5 seconds), using the same boolean threshold as the other inputs (`ADC_BOOL_THRESHOLD_V`):
- P5: `systemd_setup.sh -on` (enable/start services)
- P6: `systemd_setup.sh -off` (disable/stop services)
- P7: clear the pump controller fatal error state

## Unit templates
Unit templates live in `scripts/pump_pi_setup/systemd/`. The `systemd_setup.sh` script installs them into `/etc/systemd/system` and fills in the repo path, user, venv path, and log location.

### Example unit file
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

## Logs
```bash
tail -f ~/pump_controller.log
journalctl -u sugar-pump-controller.service -f
```
Log rotation is installed by `systemd_setup.sh -on` at `/etc/logrotate.d/sugar-pump` (2MB, keep 5).

## Environment setup
1) Copy the pump config template and edit it:
```bash
cp /home/pump/sugar_house_monitor/config/example/pump_pi.env /home/pump/sugar_house_monitor/config/pump_pi.env
```

2) Create the venv and install dependencies:
```bash
/home/pump/sugar_house_monitor/scripts/pump_pi_setup/setup_environment.sh
```

## Debug logging
Set `VERBOSE=true` in `config/pump_pi.env` to force periodic signal summaries even when all inputs are low.

## Optional hard-stop on service exit
If you want an explicit GPIO off on service stop, add this to the pump controller unit:
```ini
ExecStopPost=/usr/bin/raspi-gpio set 17 op dl
```
