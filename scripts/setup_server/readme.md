# Server setup (WordPress host)

This doc assumes the server hosts the WordPress site and serves the repo's `web/` directory via a symlink.

## Clone location
Clone the repo into your home directory:
```bash
git clone https://github.com/jeremymatt/sugar_house_monitor.git ~/sugar_house_monitor
```

## Configure server env
Copy the example env file and edit it:
```bash
cp ~/sugar_house_monitor/config/example/server.env ~/sugar_house_monitor/config/server.env
```
Update the paths in `config/server.env` (DB locations, export dir, status JSON path).

## Symlink the web assets into WordPress
Create a symlink from the WordPress document root to the repo’s `web/` folder.

1) Find the WordPress document root (where `wp-content/` lives).
2) Create the symlink from that directory:
```bash
ln -s /home/dh_m958u5/sugar_house_monitor/web /path/to/wordpress_root/sugar_house_monitor
```
After creation, the symlink should look like this:
```
lrwxrwxrwx 1 {username} {usergroup} 43 Nov 16 16:03 sugar_house_monitor -> /home/{username}/sugar_house_monitor/web
```

## Do you need a Python venv?
The server-triggered scripts (`scripts/process_status.py`, `scripts/export_db_to_csv.py`) use only the Python standard library, so **a venv is not required** for the default server workflow.

If you run any other Python services on the server (for example `scripts/web_app.py` or tank replay), you may want a venv. In that case, use the appropriate setup folder (tank/pump) or add a server-specific `requirements.txt` and `setup_environment.sh` here.

## How to check what runs server-side
- Check for systemd services:
```bash
systemctl list-units --type=service | grep sugar
```
- Check user + system crontabs:
```bash
crontab -l
ls /etc/cron.*
```
- Check for Python processes:
```bash
ps aux | grep -E "python|process_status" | grep -v grep
```
- Look for PHP calls that trigger Python (in `web/api/`):
```bash
grep -RIn "process_status.py" ~/sugar_house_monitor/web/api
```
