# Display Pi setup

## Overview
The Display Pi renders a fullscreen status dashboard (pygame) from the server status JSON and history endpoints.

## Setup
1) Create the venv and install dependencies:
```bash
/home/pump/sugar_house_monitor/scripts/display_pi_setup/setup_environment.sh
```

2) (Optional) Copy the env template and edit it:
```bash
cp /home/pump/sugar_house_monitor/config/example/display_pi.env /home/pump/sugar_house_monitor/config/display_pi.env
```
The systemd unit loads config/display_pi.env automatically when present.

3) Install and enable the service:
```bash
sudo /home/pump/sugar_house_monitor/scripts/display_pi_setup/systemd_setup.sh -on
```

## Controls summary
- systemd_setup.sh flags:
  - -on: install/update units and enable auto-restart (production).
  - -off: stop service and disable auto-restart (testing).
- Manual run in testing mode:
```bash
sudo /home/pump/sugar_house_monitor/scripts/display_pi_setup/systemd_setup.sh -off
python /home/pump/sugar_house_monitor/scripts/main_display.py
```

## Hardware overview
- Raspberry Pi connected to a display via HDMI.
- Network access to the WordPress host (for status JSON and history endpoints).

Wiring diagram:
```
Raspberry Pi ---- HDMI ---- Display
Raspberry Pi ---- Ethernet/WiFi ---- Network
Raspberry Pi ---- 5V power
```

## Additional details
- Key env vars: DISPLAY_API_BASE, DISPLAY_REFRESH_SEC, DISPLAY_HISTORY_SCOPE, NUM_PLOT_BINS.
- By default the display uses server-side display settings (scope=display). Manage those from /sugar_house_monitor/shm_admin/ on the server.
- Optional snapshots: DISPLAY_SNAPSHOT_PATH and DISPLAY_SNAPSHOT_INTERVAL_SEC.
- Unit template lives in scripts/display_pi_setup/systemd/sugar-display.service.
- Logs:
```bash
tail -f ~/display_controller.log
journalctl -u sugar-display.service -f
```

## Error info
- Network/API failures show up in the service logs; the display continues to render the most recent data it can fetch.
- Systemd restarts the service on crash; check the journal for repeated failures.
