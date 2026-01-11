# Tank Pi systemd setup

This folder documents the tank controller setup with a single systemd service.

## Quick start
Production mode (enable auto-restart):
```bash
sudo /home/pump/sugar_house_monitor/scripts/tank_pi_setup/systemd_setup.sh -on
```

Testing mode (disable auto-restart, run manually):
```bash
sudo /home/pump/sugar_house_monitor/scripts/tank_pi_setup/systemd_setup.sh -off
python /home/pump/sugar_house_monitor/scripts/main_tank.py
```

## Unit template
The unit template lives in `scripts/tank_pi_setup/systemd/sugar-tank.service`. The setup script installs it into `/etc/systemd/system` and fills in the repo path, user, venv path, and log location.

## Logs
```bash
tail -f ~/tank_controller.log
journalctl -u sugar-tank.service -f
```
Log rotation is installed by `systemd_setup.sh -on` at `/etc/logrotate.d/sugar-tank` (2MB, keep 5).

## Environment setup
1) Copy the tank config template and edit it:
```bash
cp /home/pump/sugar_house_monitor/config/example/tank_pi.env /home/pump/sugar_house_monitor/config/tank_pi.env
```

2) Create the venv and install dependencies:
```bash
/home/pump/sugar_house_monitor/scripts/tank_pi_setup/setup_environment.sh
```

## Debug timing log
Set `DEBUG_SAMPLE_PROCESS_TIMING=true` in `config/tank_pi.env` to enable writing `data/sample_process_time.csv`.
