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

## Quick start
Production mode (enable auto-restart):
```bash
sudo /home/pump/sugar_house_monitor/scripts/pump_pi_setup/systemd_setup.sh -on
```

Testing mode (disable auto-restart, run manually):
```bash
sudo /home/pump/sugar_house_monitor/scripts/pump_pi_setup/systemd_setup.sh -off
python /home/pump/sugar_house_monitor/scripts/main_pump.py
```

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
