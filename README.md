# Sugar house monitor
Planned functionality includes
1. Monitoring sap levels in each tank
1. Projecting time to full/empty
1. Monitoring the vacuum in the line system
1. Montitoring the stack temperature


# Install/setup notes:
1. Enable UART (hardware serial) with raspi-config:
    * sudo raspi-config nonint do_serial_cons 1
    * sudo raspi-config nonint do_serial_hw 0
1. Enable a second UART port:
    * sudo vi /boot/firmware/config.txt
    * Make sure the following is at the bottom of the file (un-comment the port  you want to use):
    ```
    enable_uart=1
    # dtoverlay=uart2   #TX: GPIO0 /   RX: GPIO1
    # dtoverlay=uart3   #TX: GPIO4 /   RX: GPIO5 
    # dtoverlay=uart4   #TX:CE01    /   RX:MISO
    dtoverlay=uart5   #TX: GPIO12 /   RX: GPIO13
    ```
    
# Setup on wordpress site
1. Clone the https://github.com/jeremymatt/sugar_house_monitor to `~/git/`
1. `ln -s ~/git/sugar_house_monitor/web \~/mattsmaplesyrup.com/sugar_house_monitor` to create a symlink from the sugar_house_monitor directory to the web directory in the git repo

## Repository layout (2024 refactor)

```
git/sugar_house_monitor/
├── config/                # gitignored real env files + tracked README/examples
├── data/                  # local + server SQLite DBs, exports, logs (ignored)
├── design/                # planning docs, db/api references
├── real_data/             # CSVs for debug replay
├── scripts/               # Python services + helpers
└── web/                   # Front-end UI + API endpoints (symlinked into WordPress)
```

## Configuration & secrets

All runtime behavior is controlled via `.env` files that sit in the gitignored `config/` folder at the repo root. Example templates live under `config/example/`; copy them to `config/*.env` on each device (Tank Pi, Pump Pi, server) and adjust paths if needed.

- The Python side loads config through `scripts/config_loader.py`.
- PHP endpoints read the same env files via `web/api/common.php`.
- Never commit populated env files; `.gitignore` already blocks `config/*.env`.

### Generating the shared API key

Run once on a trusted machine:

```
cd ~/git/sugar_house_monitor
python3 scripts/gen_credentials.py
```

The helper will ask for the public server URL, create `config/` if it does not exist, generate a cryptographically strong API key, and populate `config/server.env`, `config/tank_pi.env`, and `config/pump_pi.env`. Copy the relevant file to each device (e.g., `scp config/tank_pi.env tankpi:/home/pi/sugar_house_monitor/config/`).

### Environment variables of note

| File | Key | Purpose |
|------|-----|---------|
| `tank_pi.env` | `API_BASE_URL` | HTTPS endpoint for uploads |
| "" | `API_KEY` | Shared auth key for both ingest endpoints |
| "" | `DEBUG_TANK`, `DEBUG_RELEASER` | Enable CSV replay using the synthetic clock |
| "" | `SYNTHETIC_CLOCK_MULTIPLIER` | Speed multiplier for debug playback |
| "" | `DEBUG_LOOP_DATA` | Loop through the sample CSVs continuously |
| "" | `DEBUG_LOOP_GAP_SECONDS` | Pause between debug loop cycles (default 10s) |
| "" | `RESET_ON_DEBUG_START` | When true, deletes both local/server DBs before replay |
| "" | `UPLOAD_BATCH_SIZE` / `UPLOAD_INTERVAL_SECONDS` | Tank reading batching cadence |
| "" | `PUMP_UPLOAD_BATCH_SIZE` / `PUMP_UPLOAD_INTERVAL_SECONDS` | Pump event upload cadence (keep batch size = 1) |
| "" | `LOCAL_HTTP_PORT` / `WEB_ROOT` | Host the `web/` directory locally while in debug |
| `server.env` | `TANK_DB_PATH`, `PUMP_DB_PATH` | SQLite files the ingest PHP scripts write to |
| "" | `STATUS_JSON_PATH` | Path within `web/data/`; its parent directory stores the per-component `status_*.json` files |
| "" | `EXPORT_DIR` | Destination for CSV exports |

### Local debug replay (Tank Pi)

To preview the UI without any field hardware:

1. Copy or symlink samples in `real_data/` and set `BROOKSIDE_CSV`, `ROADSIDE_CSV`, and `PUMP_EVENTS_CSV` in `config/tank_pi.env`.
2. Enable `DEBUG_TANK=true` (and optionally `DEBUG_RELEASER=true`) and pick a `SYNTHETIC_CLOCK_MULTIPLIER`.
3. Keep `RESET_ON_DEBUG_START=true` when you want the Pi to wipe its local SQLite DB and call `/api/reset.php` so the server DBs/status files are pristine before replay.
4. Run `python3 scripts/main.py`. The service will:
   - Recreate the per-component `web/data/status_*.json` files based on the local queue (for the fallback UI) and simultaneously POST batches to `ingest_tank.php` / `ingest_pump.php`, just like the live sensors.
   - Serve the `web/` directory via `http://<pi-host>:<LOCAL_HTTP_PORT>/`. Because the server WordPress instance is also receiving the synthetic data, both sites should show identical telemetry, and CSV exports from `scripts/export_db_to_csv.py` will reflect the replay.
5. Leave `DEBUG_LOOP_DATA=true` if you want continuous playback once the newest sample is reached.

Tank Pi batching is configurable via `UPLOAD_BATCH_SIZE`, while pump events always upload immediately (`PUMP_UPLOAD_BATCH_SIZE=1`).

## Data flow overview

1. Tank Pi / Pump Pi sample hardware, write to local SQLite queues, and POST processed readings/events to `web/api/ingest_tank.php` or `web/api/ingest_pump.php`.
2. Each ingest script validates the API key, stores rows in `data/*_server.db`, triggers `python3 scripts/process_status.py`, and responds with an ACK (latest timestamp per stream).
3. `scripts/process_status.py` composes the per-component `web/data/status_*.json` files, which power both the WordPress UI and the Tank Pi fallback UI.
4. `scripts/export_db_to_csv.py` can be run manually to dump long-term history from the server DBs into `data/exports/`.

See `design/plan.md` §13 for the current implementation roadmap and device-specific notes.
