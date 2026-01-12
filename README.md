# Sugar House Monitor

Live/Simulated monitoring for Brookside/Roadside tanks, transfer pump, evaporator flow, and vacuum with three Pis and a WordPress-hosted UI.

## Data Flow (current)
- Tank Pi + Pump Pi (or Tank Pi debug replay) write readings/events locally, then POST to WordPress APIs (`web/api/ingest_tank.php`, `ingest_pump.php`, `ingest_vacuum.php`).
- Each ingest call updates the server SQLite DBs and triggers `scripts/process_status.py`, which emits `web/data/status_*.json` (including `status_evaporator.json`) from the latest records.
- Frontend (`web/index.html` + `web/js/app.js`) and Display Pi (`scripts/main_display.py`) read the status JSON plus history APIs (`flow_history.php`, `evaporator_history.php`).
- CSV exports are available via `web/api/export_csv.php` and `/downloads.html`.

## Device Roles
### Tank Pi
- Two ultrasonic sensors (Brookside/Roadside); computes volume/flow/ETA locally.
- Queues to `tank_pi.db`; uploads via shared API key.
- Debug: replays `real_data/brookside.csv` / `roadside.csv` (and optionally pump CSV) with SyntheticClock.
- Hosts local fallback UI from `web/`.

### Pump Pi
- Captures pump start/stop events, computes run/interval/gph, uploads to server (`pump_events` table).
- Debug is handled by Tank Pi using `real_data/pump_times.csv`.

#### Pump Pi state machine (P1/P2/P3 truth table)
- P1 = tank_full input, P2 = manual_start input, P3 = tank_empty input; states are `pumping`, `manual_pumping`, and `not_pumping`.
- `error_count` increments while an error condition persists and resets when signals clear; if `error_count >= error_threshold`, the controller forces a fatal stop.

| P1 | P2 | P3 | Error state | Error action | Current state | Next state / action |
|----|----|----|-------------|--------------|---------------|---------------------|
| any | any | any | error_count >= error_threshold | no action | `ERROR_STATE` | `not_pumping` / stop loop, flag error on WordPress, set `error_message="FATAL ERROR: STOPPING"`, write/queue error log |
| 0 | 0 | 0 | error_count < error_threshold | reset `error_count=0` | `pumping` | `pumping` / no action |
| 0 | 0 | 0 | error_count < error_threshold | reset `error_count=0` | `manual_pumping` | `manual_pumping` / no action |
| 0 | 0 | 0 | error_count < error_threshold | reset `error_count=0` | `not_pumping` | `not_pumping` / no action |
| 1 | 0 | 0 | error_count < error_threshold | `error_count += 1` | `pumping` | `pumping` / set `error_message="WARNING: received tank full signal while auto pumping"` and write/queue error |
| 1 | 0 | 0 | error_count < error_threshold | `error_count += 1` | `manual_pumping` | `pumping` / set `error_message="WARNING: received tank full signal while manual pumping"`, write/queue error, run `tank_full_event_handling()` |
| 1 | 0 | 0 | error_count < error_threshold | reset `error_count=0` | `not_pumping` | `pumping` / run `tank_full_event_handling()` |
| 0 | 1 | 0 | error_count < error_threshold | reset `error_count=0` | `pumping` | `pumping` / set `error_message="WARNING: received manual pump signal while auto pumping"` and write/queue error |
| 0 | 1 | 0 | error_count < error_threshold | reset `error_count=0` | `manual_pumping` | `manual_pumping` / no action |
| 0 | 1 | 0 | error_count < error_threshold | reset `error_count=0` | `not_pumping` | `manual_pumping` / record event locally, queue to WordPress, set `pump_end_time=None`, set `pump_start_time=time.time()` if missing |
| 0 | 0 | 1 | error_count < error_threshold | reset `error_count=0` | `pumping` | `not_pumping` / calculate pump time, record locally, queue to WordPress, set `pump_end_time=time.time()` |
| 0 | 0 | 1 | error_count < error_threshold | reset `error_count=0` | `manual_pumping` | `not_pumping` / record locally, queue to WordPress, set `pump_end_time=time.time()` |
| 0 | 0 | 1 | error_count < error_threshold | reset `error_count=0` | `not_pumping` | `not_pumping` / set `pump_end_time=time.time()` |
| 1 | 1 | 0 | error_count < error_threshold | `error_count += 1` | `pumping` | `pumping` / set `error_message="WARNING: received simultaneous tank full and manual start signals while auto pumping"` and queue to WordPress |
| 1 | 1 | 0 | error_count < error_threshold | `error_count += 1` | `manual_pumping` | `pumping` / set `error_message="WARNING: received simultaneous tank full and manual start signals while manually pumping"`, queue to WordPress, run `tank_full_event_handling()` |
| 1 | 1 | 0 | error_count < error_threshold | `error_count += 1` | `not_pumping` | `pumping` / set `error_message="WARNING: received simultaneous tank full and manual start signals while not pumping"`, queue to WordPress, run `tank_full_event_handling()` |
| 1 | 0 | 1 | error_count < error_threshold | `error_count += 1` | `pumping` | `pumping` / set `error_message="ERROR: received simultaneous tank empty and tank full signals while auto pumping"`, write/queue error |
| 1 | 0 | 1 | error_count < error_threshold | `error_count += 1` | `manual_pumping` | `manual_pumping` / set `error_message="ERROR: received simultaneous tank empty and tank full signals while manual pumping"`, write/queue error |
| 1 | 0 | 1 | error_count < error_threshold | `error_count += 1` | `not_pumping` | `pumping` / set `error_message="ERROR: received simultaneous tank empty and tank full signals while not pumping"`, write/queue error |
| 0 | 1 | 1 | error_count < error_threshold | reset `error_count=0` | `pumping` | `not_pumping` / set `error_message="WARNING: received simultaneous tank empty and manual pump start signals while auto pumping"`, write/queue error |
| 0 | 1 | 1 | error_count < error_threshold | reset `error_count=0` | `manual_pumping` | `not_pumping` / set `error_message="WARNING: received simultaneous tank empty and manual pump start signals while manual pumping"`, write/queue error |
| 0 | 1 | 1 | error_count < error_threshold | reset `error_count=0` | `not_pumping` | `not_pumping` / set `error_message="WARNING: received simultaneous tank empty and manual pump start signals while not pumping"`, write/queue error |
| 1 | 1 | 1 | error_count < error_threshold | `error_count += 1` | `pumping` | `pumping` / set `error_message="ERROR: received simultaneous tank empty, manual start, and tank full signals while auto pumping"`, write/queue error |
| 1 | 1 | 1 | error_count < error_threshold | `error_count += 1` | `manual_pumping` | `manual_pumping` / set `error_message="ERROR: received simultaneous tank empty, manual start, and tank full signals while manual pumping"`, write/queue error |
| 1 | 1 | 1 | error_count < error_threshold | `error_count += 1` | `not_pumping` | `pumping` / set `error_message="ERROR: received simultaneous tank empty, manual start, and tank full signals while not pumping"`, write/queue error |

- `tank_full_event_handling()`: sets `pump_start_time` if missing; when `pump_end_time` is present, computes `fill_time` and `flow_rate`, records an event, and clears `pump_end_time`; otherwise logs a warning that `pump_end_time` was missing.

### Display Pi
- Pygame fullscreen display (`scripts/main_display.py`) reading `status_evaporator.json` + `evaporator_history.php`.
- Uses server plot settings (window/y-limits) and shows colored evaporator flow by draw-off tank.

## Install/setup notes (Tank Pi)
1. Enable UART (hardware serial) with raspi-config:
    * `sudo raspi-config nonint do_serial_cons 1`
    * `sudo raspi-config nonint do_serial_hw 0`
1. Enable a second UART port:
    * `sudo vi /boot/firmware/config.txt`
    * Ensure one `dtoverlay` is enabled:
    ```
    enable_uart=1
    # dtoverlay=uart2   #TX: GPIO0 /   RX: GPIO1
    # dtoverlay=uart3   #TX: GPIO4 /   RX: GPIO5 
    # dtoverlay=uart4   #TX:CE01    /   RX:MISO
    dtoverlay=uart5   #TX: GPIO12 /   RX: GPIO13
    ```
    
## Setup on WordPress site
1. Clone https://github.com/jeremymatt/sugar_house_monitor to `~/git/`
1. `ln -s ~/git/sugar_house_monitor/web ~/mattsmaplesyrup.com/sugar_house_monitor` to expose the web assets/API under the site

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
4. Run `python3 scripts/main_tank.py`. The service will:
   - Recreate the per-component `web/data/status_*.json` files based on the local queue (for the fallback UI) and simultaneously POST batches to `ingest_tank.php` / `ingest_pump.php`, just like the live sensors.
   - Serve the `web/` directory via `http://<pi-host>:<LOCAL_HTTP_PORT>/`. Because the server WordPress instance is also receiving the synthetic data, both sites should show identical telemetry, and CSV exports from `scripts/export_db_to_csv.py` will reflect the replay.
5. Leave `DEBUG_LOOP_DATA=true` if you want continuous playback once the newest sample is reached.

Tank Pi batching is configurable via `UPLOAD_BATCH_SIZE`, while pump events always upload immediately (`PUMP_UPLOAD_BATCH_SIZE=1`).

## Data flow overview

1. Tank Pi / Pump Pi sample hardware (or replay CSV), write to local SQLite queues, and POST processed readings/events to `web/api/ingest_tank.php`, `ingest_pump.php`, and `ingest_vacuum.php`.
2. Ingest scripts validate API key, upsert into `data/*_server.db`, trigger `scripts/process_status.py`, and ACK with latest timestamps.
3. `scripts/process_status.py` composes per-component `web/data/status_*.json` (including evaporator placeholder if no data yet) and keeps plot settings in `data/evaporator.db`.
4. `flow_history.php` / `evaporator_history.php` serve chart history; `export_csv.php` and `/downloads.html` provide full CSV exports.

See `design/plan.md` §13/§14 for roadmap and status.
