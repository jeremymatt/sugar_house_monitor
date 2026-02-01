# Server setup (WordPress host)

## Overview
The server hosts the WordPress site, serves the repo's web/ assets via a symlink, and runs the ingest APIs that trigger scripts/process_status.py.

## Setup
1) Clone the repo:
```bash
git clone https://github.com/jeremymatt/sugar_house_monitor.git ~/sugar_house_monitor
```

2) Copy the env template and edit it:
```bash
cp ~/sugar_house_monitor/config/example/server.env ~/sugar_house_monitor/config/server.env
```
Update DB paths, export directory, and status JSON path in config/server.env.

3) Symlink the web assets into WordPress:
```bash
ln -s /home/<user>/sugar_house_monitor/web /path/to/wordpress_root/sugar_house_monitor
```

## Controls summary
- There is no systemd_setup.sh for the server by default; PHP ingest endpoints trigger scripts/process_status.py after uploads.
- If you add any custom cron/systemd jobs, document them locally and ensure they run under the same repo + config paths.

## Hardware overview
- No special hardware requirements beyond the WordPress host.

Wiring diagram:
```
N/A (server-side only)
```

## Additional details
- The default server scripts use only the Python standard library; a venv is not required unless you run other services.
- To check what runs server-side:
```bash
systemctl list-units --type=service | grep sugar
crontab -l
ls /etc/cron.*
ps aux | grep -E "python|process_status" | grep -v grep
```
- The ingest endpoints live under web/api/ and accept the shared API key in the X-API-Key header or api_key field.

## Error info
- PHP errors will surface in the web server logs; check the WordPress/PHP error log first.
- If status JSON files are missing, confirm process_status.py can read config/server.env paths and has write access to web/data/.
