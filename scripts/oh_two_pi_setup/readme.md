# O2 Pi systemd setup

This folder provides systemd units and setup scripts for the O2 monitor Pi.

## Quick start
Production mode (enable auto-restart):
```bash
sudo /home/pi/sugar_house_monitor/scripts/oh_two_pi_setup/systemd_setup.sh -on
```

Testing mode (stop service, disable auto-restart):
```bash
sudo /home/pi/sugar_house_monitor/scripts/oh_two_pi_setup/systemd_setup.sh -off
```

## Environment setup
1) Copy the config template and edit it:
```bash
cp /home/pi/sugar_house_monitor/config/example/oh_two_pi.env /home/pi/sugar_house_monitor/config/oh_two_pi.env
```

2) Create the venv and install dependencies:
```bash
/home/pi/sugar_house_monitor/scripts/oh_two_pi_setup/setup_environment.sh
```

## Logs
```bash
tail -f ~/oh_two.log
journalctl -u sugar-oh-two.service -f
```
Log rotation is installed by `systemd_setup.sh -on` at `/etc/logrotate.d/sugar-oh-two` (2MB, keep 5).

## Status LED
- Solid on: service running.
- Blinking (1 Hz): error state detected in sampling or upload.
- Off: service stopped.
