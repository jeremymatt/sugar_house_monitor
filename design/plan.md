# 🍁 Sugar House Monitor — Master Architecture & Refactor Plan

**Goal:**  
Modernize and restructure the Sugar House Monitor system across three Raspberry Pis and a WordPress-hosted webserver, while retaining verified tank-geometry logic and enabling robust, loss-resistant data reporting.

This document captures the complete design from ChatGPT planning.

---

# 1. System Overview

There will be **three Raspberry Pis**:

## A. Tank Pi  
- Reads **two ultrasonic sensors** (Brookside & Roadside).
- Uses existing **verified geometry code** for volumes.
- Computes:
  - depth  
  - volume (gallons)  
  - flow rate  
  - time-to-full / time-to-empty  
- Stores readings locally in **tank_pi.db** (SQLite).
- Uploads processed readings to the server using **one shared API key**.
- Serves a **local backup web UI** identical to server’s UI.
- Supports both:
  - `DEBUG_TANK`: replay tank CSV data with SyntheticClock.
  - `DEBUG_RELEASER`: replay pump events CSV from the same Pi.

Tank debug CSV filenames:
- `real_data/brookside.csv`
- `real_data/roadside.csv`

Format:
```
timestamp,yr,mo,day,hr,m,s,surf_dist,depth,gal
```

## B. Pump Pi  
- Reads pump events (“Pump Start”, “Pump Stop”, etc).
- Stores events in **pump_pi.db**.
- Computes:
  - Pump_Run_Time  
  - Pump_Interval  
  - Gallons_Per_Hour  
- Uploads processed events to server.
- **No debug mode**, because tank Pi handles debug replay.

Pump debug CSV used *by tank Pi* when `DEBUG_RELEASER = True`:
- `real_data/pump_times.csv`

CSV headers include:
```
Time,Pump_Event,Pump_Run_Time,Pump_Interval,Gallons_Per_Hour
```

## C. Display Pi  
- Pi Zero with a small screen.
- Loads the per-component `status_*.json` files from the server and displays values visually.
- Will likely be implemented in pygame.

---

# 2. WordPress Server Architecture

Directory structure (under user home):

```
~/git/sugar_house_monitor/
  web/          ← symlinked to WordPress ~/mattsmaplesyrup.com/sugar_house_monitor
  scripts/      ← Python logic (not web-exposed)
  data/         ← SQLite DBs, CSV export outputs
  real_data/    ← for debug playback
```

### Web-visible
```
/sugar_house_monitor/
  index.html
  js/app.js
  data/status_brookside.json
  data/status_roadside.json
  data/status_pump.json
  api/ingest_tank.php
  api/ingest_pump.php
```

### Private (NOT web-visible)
```
scripts/process_status.py
scripts/export_db_to_csv.py
data/tank_server.db
data/pump_server.db
```

Data processing is triggered on **data upload**, not via cron.

---

# 3. Database Design (SQLite Everywhere)

Use a **separate DB file per sensor type** for extensibility.

## 3.1 tank_pi.db (on Tank Pi)

| Field             | Purpose |
|-------------------|---------|
| id (PK)           | |
| timestamp         | measurement timestamp (from Pi) |
| tank_id           | "brookside" or "roadside" |
| surf_dist         | distance from sensor |
| depth             | depth of liquid |
| volume_gal        | computed using geometry code |
| flow_gph          | computed locally |
| eta_full          | ISO timestamp or null |
| eta_empty         | ISO timestamp or null |
| sent_to_server    | 0/1 |
| acked_by_server   | 0/1 |

## 3.2 pump_pi.db (on Pump Pi)

| Field | Purpose |
|--------|---------|
| timestamp | same format as Pi |
| event_type | “Pump Start”, “Pump Stop”, etc |
| pump_run_time_s | computed locally |
| pump_interval_s | computed locally |
| gallons_per_hour | computed locally |
| sent_to_server | 0/1 |
| acked_by_server | 0/1 |

## 3.3 tank_server.db & pump_server.db (on server)

Same fields as Pis *plus*:

- `received_at` (server timestamp)

No sent/acked fields on the server.

---

# 4. Upload Logic (Robust Against Network Loss)

Each Pi uses a **local SQLite queue**:

1. Every reading/event inserted locally.
2. A sync loop periodically:
   - selects rows where `acked_by_server = 0`
   - sends a JSON batch to server endpoint
   - server returns list of acked row IDs
   - Pi updates those rows to `acked_by_server = 1`
3. If server unreachable → data remains → retry later → **no data loss, including if pi shuts down due to loss of power**.

---

# 5. Processing Strategy (All Data Processing Happens on Pis)

To avoid duplicate logic:

- **Tank Pi** computes (see the existing ~/sugar_house_monitor/scripts/tank_vol_fcns.py):
  - volume_gal  <- the functions calculating sap volume in the tanks *MUST NOT* change
  - flow_gph  <- This can change if there's a better way to do it
  - eta_full / eta_empty  <- This can change if there's a better way to do it

- **Pump Pi** computes (ignore this for now - we'll develop this code later):
  - pump_run_time_s  
  - pump_interval_s  
  - gallons_per_hour  

- **Server does NOT recompute any of this.**
- Server only:
  - Saves readings,
  - Records `received_at`,
  - Publishes `status.json`.

---

# 6. Staleness Tracking (Client-side)

Different sensors have different expected update intervals, so staleness must be per-stream.

Server inserts `last_received_at` (server timestamp) into each tank/pump record.

Frontend JS computes:

- `secondsSinceLast = now - last_received_at`
- Checks staleness using thresholds:

```js
STALE_THRESHOLDS = {
  tank_brookside: 120,
  tank_roadside: 120,
  pump: 7200
};
```

UI updates every 5 seconds via JS `setInterval`.

---

# 7. Shared Frontend UI

The **same UI** is served:

- From WordPress server  
- From Tank Pi’s Flask server for local fallback

Provided files:

- `web/index.html` (already provided)
- `web/js/app.js` (already provided)

What the frontend displays:

- Tank volumes with animated level bars  
- Flow rates  
- ETAs for full/empty  
- Pump events  
- Human-readable “last updated X seconds ago”  
- Fresh vs stale status indicators

---

# 8. Debug / Replay Mode (On Tank Pi Only)

## DEBUG_TANK mode
- Uses CSV:
  - `real_data/brookside.csv`
  - `real_data/roadside.csv`
- SyntheticClock controls simulated time.
- Reads are emitted at:
  ```
  synthetic_time >= next_csv_timestamp
  ```

## DEBUG_RELEASER mode
- Uses:
  - `real_data/pump_times.csv`
- Handled entirely on Tank Pi.
- Simulates pump events and uploads them just like the Pump Pi.

This allows testing *all* streams with **one device**.

---

# 9. API Endpoints (PHP)

Under `web/api/`:

### ingest_tank.php
- Checks API key.
- Reads JSON array of processed tank readings.
- Inserts into `tank_server.db` with `received_at=NOW()`.
- Calls:
  ```
  python3 scripts/process_status.py
  ```

### ingest_pump.php
- Similar behavior but writes pump data into `pump_server.db`.

Both scripts fully rely on processed values sent by Pis.

---

# 10. process_status.py (Server)

When triggered:

1. Load **latest tank readings** per tank from tank_server.db.
2. Load **latest pump event** from pump_server.db.
3. Write `web/data/status.json`:

```json
{
  "generated_at": "...",
  "tanks": {...},
  "pump": {...}
}
```

status.json fields include:

- tank_id  
- volume_gal  
- capacity_gal  
- level_percent  
- flow_gph  
- eta_full  
- eta_empty  
- last_sample_timestamp (from Pi)  
- last_received_at (server)  

Pump section includes:

- event_type  
- pump_run_time_s  
- pump_interval_s  
- gallons_per_hour  
- last_event_timestamp  
- last_received_at  

---

# 11. CSV Export (On-demand Only)

Utility script:

```
python3 scripts/export_db_to_csv.py
```

Generates:

```
data/exports/tank_readings.csv
data/exports/pump_events.csv
```

Add download links later if desired.

No cron jobs needed.

---

# 12. Recommended VS Code / Codex Workflow

1. Create a folder `/design`:
   - `plan.md` (this document)
   - `db_schema.md`
   - `api_format.md`

2. Paste this entire file into `plan.md`.

3. Give Codex:
   - This plan
   - The existing code (especially tank_vol_fcns.py)
   - Instructions about which file you want generated next

4. Codex can now:
   - Refactor tank_vol_fcns.py without changing geometry logic
   - Build Tank Pi / Pump Pi main scripts
   - Implement ingestion PHP files
   - Implement process_status.py
   - Build Flask-based fallback UI for Tank Pi
   - Implement SQLite queue code
   - Implement debug replay scripts

This gives Codex a complete system map to work with.

---

# 13. 2024 Implementation Roadmap

All work-items below assume **headless services** (no CLI flags) whose behavior is entirely controlled through `.env` files that live inside a gitignored `config/` folder.

## 13.1 Config + credential management
- Track documentation and examples inside `config/README.md` & `config/example/*.env`.
- Real configs live in `config/*.env` (gitignored) and are loaded by every Python + PHP component via `scripts/config_loader.py` and `web/api/common.php`.
- A new helper (`scripts/gen_credentials.py`) generates shared API keys and writes out initial env files for Tank Pi, Pump Pi, and the server. It prompts for server URL once and derives sensible default paths (e.g., `../data/tank_server.db`). Reruns keep existing files unless the operator chooses to overwrite.

## 13.2 Tank Pi service stack
- Wrap the proven geometry logic in a sampler daemon that:
  - Reads both ultrasonic sensors at the configured cadence.
  - Stores raw + derived measurements in `tank_pi.db`.
  - Runs a resend/ACK queue that posts JSON batches to `web/api/ingest_tank.php`. ACKs are based on latest `source_timestamp` per tank and stored locally.
- Host a lightweight fallback UI on Tank Pi that simply serves the exact contents of `web/` (status page + JS bundle + `data/status.json`), so field operators get the same visuals even if the WAN link is down.

## 13.3 Synthetic clock + debug replay
- Implement `scripts/synthetic_clock.py`, which:
  - Scans enabled CSVs (`real_data/*.csv`) on startup to find the earliest timestamp.
  - Sets the simulated “now” to that instant and advances time at the configured multiplier (default 2× or 10×).
  - Emits rows through a generator whenever synthetic time passes their timestamp, letting Tank Pi exercise the full pipeline (including pump replay) without any CLI switches—just flip `DEBUG_TANK=true` / `DEBUG_RELEASER=true` inside the env file.

## 13.4 Pump Pi integration prep
- Abstract the uploader/queue logic into a small library so Pump Pi can re-use it when its existing GPIO monitor is refit for the new server API.
- For now, only ensure `pump_pi.db` schema + ingestion endpoint mirror today’s event format (start/stop, run time, interval, gallons/hour) so the eventual Pump Pi update is just a client swap.

## 13.5 Server ingestion + processing
- Build shared PHP glue (`web/api/common.php`) that loads the server env, validates the shared API key (from HTTP header), and exposes helpers for JSON responses and SQLite connections.
- Implement `web/api/ingest_tank.php` / `ingest_pump.php`:
  - Expect JSON arrays of processed readings/events.
  - Upsert into `data/tank_server.db` / `data/pump_server.db` with `UNIQUE(source_timestamp, tank_id)` guards so harmless resends are deduped.
  - Immediately call `python3 scripts/process_status.py` (run in the repo root) so every accepted payload refreshes `web/data/status.json`.
  - Reply with `{status:"ok", accepted:n, last_timestamp:{...}}` as the ACK that the Pis look for.
- `scripts/process_status.py`:
  - Loads latest record per tank + the latest pump event.
  - Computes the exact JSON schema expected by `web/js/app.js` (volume, percent, eta, flow, staleness metadata, etc.).
  - Writes to `web/data/status.json` atomically (temp file + rename) and records `generated_at`.

## 13.6 Export + tooling
- `scripts/export_db_to_csv.py` dumps both server DBs to `data/exports/*.csv` on demand, taking all parameters (paths, export dir, optional time window) from the server env file.
- Future systemd services and log rotation scripts can live under `scripts/systemd/` once verified on-device.

This roadmap locks the architecture decisions captured above into concrete repository tasks so implementation can proceed feature-by-feature.

# End of plan.md
# End of plan.md
