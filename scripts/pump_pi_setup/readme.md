# Pump Pi systemd setup

This folder documents a multi-process layout so SPI access is centralized and each role can be restarted independently.

## Safety and state machine behavior
- The pump controller remains the only authority for pump on/off decisions.
- Incoherent or stale sensor data must be handled inside the pump controller: set fatal, force pump off, keep the process running.
- Supervisors should restart only on crash or hang, not on "fatal" or "warning" states.
- If the ADC service stops updating, the pump controller should treat samples as stale and keep the pump off until samples resume or an operator intervenes.

## Recommended supervision model
Systemd can supervise each process directly. A custom supervisor script is optional, but it adds a single point of failure because all children die if it crashes.

## Service layout
- sugar-adc.service: owns MCP3008 and publishes latest samples (socket, shared memory, or a small DB).
- sugar-pump-controller.service: reads cached samples and drives the relay + state machine.
- sugar-vacuum.service: computes slow vacuum averages from cached samples.
- sugar-uploader.service: uploads pump events, vacuum readings, and error logs.

## Example unit files
These ExecStart paths assume new entry points named below. Update to match the actual scripts once they exist.

### /etc/systemd/system/sugar-adc.service
```ini
[Unit]
Description=Sugar House ADC Service
After=network.target

[Service]
Type=simple
User=pump
WorkingDirectory=/home/pump/sugar_house_monitor
Environment=PYTHONUNBUFFERED=1
Environment=SUGAR_CONFIG_DIR=/home/pump/sugar_house_monitor/config
ExecStart=/home/pump/.venv/bin/python /home/pump/sugar_house_monitor/scripts/adc_service.py
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
```

### /etc/systemd/system/sugar-pump-controller.service
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

[Install]
WantedBy=multi-user.target
```

### /etc/systemd/system/sugar-vacuum.service
```ini
[Unit]
Description=Sugar House Vacuum Sampler
After=sugar-adc.service
Requires=sugar-adc.service

[Service]
Type=simple
User=pump
WorkingDirectory=/home/pump/sugar_house_monitor
Environment=PYTHONUNBUFFERED=1
Environment=SUGAR_CONFIG_DIR=/home/pump/sugar_house_monitor/config
ExecStart=/home/pump/.venv/bin/python /home/pump/sugar_house_monitor/scripts/vacuum_service.py
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
```

### /etc/systemd/system/sugar-uploader.service
```ini
[Unit]
Description=Sugar House Upload Worker
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=pump
WorkingDirectory=/home/pump/sugar_house_monitor
Environment=PYTHONUNBUFFERED=1
Environment=SUGAR_CONFIG_DIR=/home/pump/sugar_house_monitor/config
ExecStart=/home/pump/.venv/bin/python /home/pump/sugar_house_monitor/scripts/upload_service.py
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
```

### /etc/systemd/system/sugar-pump.target
```ini
[Unit]
Description=Sugar House Pump Stack
Wants=sugar-adc.service sugar-pump-controller.service sugar-vacuum.service sugar-uploader.service

[Install]
WantedBy=multi-user.target
```

## Enable the services
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now sugar-pump.target
```

## Logs
```bash
journalctl -u sugar-pump-controller.service -f
```

## Environment setup
1) Copy the pump config template and edit it:
```bash
cp /home/pump/sugar_house_monitor/config/example/pump_pi.env /home/pump/sugar_house_monitor/config/pump_pi.env
```

2) Create the venv and install dependencies:
```bash
/home/pump/sugar_house_monitor/scripts/pump_pi_setup/setup_environment.sh
```

## Optional hard-stop on service exit
If you want an explicit GPIO off on service stop, add this to the pump controller unit:
```ini
ExecStopPost=/usr/bin/raspi-gpio set 17 op dl
```
