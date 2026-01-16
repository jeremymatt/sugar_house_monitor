# Display Pi systemd setup

This folder documents the fullscreen display setup with a single systemd service.

## Quick start
Production mode (enable auto-restart):
```bash
sudo /home/pump/sugar_house_monitor/scripts/display_pi_setup/systemd_setup.sh -on
```

Testing mode (disable auto-restart, run manually):
```bash
sudo /home/pump/sugar_house_monitor/scripts/display_pi_setup/systemd_setup.sh -off
python /home/pump/sugar_house_monitor/scripts/main_display.py
```

## Unit template
The unit template lives in `scripts/display_pi_setup/systemd/sugar-display.service`. The setup script installs it into `/etc/systemd/system` and fills in the repo path, user, venv path, and log location.

## Logs
```bash
tail -f ~/display_controller.log
journalctl -u sugar-display.service -f
```
Log rotation is installed by `systemd_setup.sh -on` at `/etc/logrotate.d/sugar-display` (2MB, keep 5).

## Environment setup
Create the venv and install dependencies:
```bash
/home/pump/sugar_house_monitor/scripts/display_pi_setup/setup_environment.sh
```

## Runtime config
The display reads its configuration from environment variables (e.g. `DISPLAY_API_BASE`, `DISPLAY_REFRESH_SEC`, `NUM_PLOT_BINS`). See `scripts/main_display.py` for defaults.
If you create `config/display_pi.env`, the systemd unit will load it automatically.

By default the display uses the server-side display settings (scope=display). You can manage those from `/sugar_house_monitor/shm_admin/`.
