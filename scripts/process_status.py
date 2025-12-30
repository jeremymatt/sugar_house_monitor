"""
Generate per-component status JSON files from the latest tank + pump readings.

This script is triggered automatically by the ingest PHP endpoints.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, Optional, Tuple

from config_loader import load_role, repo_path_from_config


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return None


def should_update_status(path: Path, new_ts: Optional[str]) -> bool:
    """
    Only update a status file if the incoming timestamp is newer than what is present.
    """
    if new_ts is None:
        return False
    if not path.exists():
        return True
    try:
        current = json.loads(path.read_text())
    except Exception:
        return True
    current_ts = (
        current.get("last_event_timestamp")
        or current.get("source_timestamp")
        or current.get("last_sample_timestamp")
    )
    cur_dt = parse_iso(current_ts)
    new_dt = parse_iso(new_ts)
    if cur_dt is None or new_dt is None:
        return True
    return new_dt > cur_dt


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return cur.fetchone() is not None


def get_latest_tank_rows(conn: sqlite3.Connection) -> Dict[str, sqlite3.Row]:
    if not table_exists(conn, "tank_readings"):
        return {}
    cur = conn.execute(
        """
        SELECT tr.*
        FROM tank_readings tr
        INNER JOIN (
            SELECT tank_id, MAX(source_timestamp) AS max_source
            FROM tank_readings
            GROUP BY tank_id
        ) latest
            ON latest.tank_id = tr.tank_id AND latest.max_source = tr.source_timestamp
        """
    )
    return {row["tank_id"]: row for row in cur.fetchall()}


def get_latest_pump_row(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    if not table_exists(conn, "pump_events"):
        return None
    cur = conn.execute(
        """
        SELECT *
        FROM pump_events
        ORDER BY received_at DESC
        LIMIT 1
        """
    )
    return cur.fetchone()


def get_latest_pump_gph(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    if not table_exists(conn, "pump_events"):
        return None
    cur = conn.execute(
        """
        SELECT *
        FROM pump_events
        WHERE gallons_per_hour IS NOT NULL
        ORDER BY received_at DESC
        LIMIT 1
        """
    )
    return cur.fetchone()


def get_latest_vacuum_row(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    if not table_exists(conn, "vacuum_readings"):
        return None
    cur = conn.execute(
        """
        SELECT *
        FROM vacuum_readings
        ORDER BY received_at DESC
        LIMIT 1
        """
    )
    return cur.fetchone()


def get_latest_stack_temp_row(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    if not table_exists(conn, "stack_temperatures"):
        return None
    cur = conn.execute(
        """
        SELECT *
        FROM stack_temperatures
        ORDER BY received_at DESC
        LIMIT 1
        """
    )
    return cur.fetchone()


def get_latest_monitor_ts(conn: sqlite3.Connection, stream: str) -> Optional[str]:
    if not table_exists(conn, "monitor_heartbeats"):
        return None
    cur = conn.execute(
        """
        SELECT last_received_at
        FROM monitor_heartbeats
        WHERE stream = ?
        ORDER BY last_received_at DESC
        LIMIT 1
        """,
        (stream,),
    )
    row = cur.fetchone()
    return row["last_received_at"] if row else None


def calc_percent(volume: Optional[float], capacity: Optional[float]) -> Optional[float]:
    if volume is None or capacity in (None, 0):
        return None
    return max(0.0, min(100.0, (float(volume) / float(capacity)) * 100.0))


def atomic_write(path: Path, payload: Dict) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path.write_text(json.dumps(payload, indent=2))
    tmp_path.replace(path)


# ---- Evaporator flow helpers ----

PRUNE_MARKER_FILENAME = "last_server_prune.txt"
DEFAULT_RETENTION_CHECK_SECONDS = 24 * 60 * 60
EST_TZ = timezone(timedelta(hours=-5))  # Fixed offset; aligns with EST
DEFAULT_EMPTYING_THRESHOLD = -2.5
ZERO_FLOW_TOLERANCE = 2.5
PUMP_LOW_THRESHOLD = 5.0
PUMP_MATCH_TOLERANCE = 0.5
DEFAULT_PLOT_SETTINGS = (200.0, 600.0, 2 * 60 * 60)  # y_min, y_max, window_sec
NO_TANK = "---"


def parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
    except ValueError:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def average_timestamp_iso(ts_a: Optional[str], ts_b: Optional[str]) -> Optional[str]:
    dt_a = parse_iso(ts_a)
    dt_b = parse_iso(ts_b)
    if dt_a and dt_b:
        avg = (dt_a.timestamp() + dt_b.timestamp()) / 2.0
        return datetime.fromtimestamp(avg, tz=timezone.utc).isoformat()
    if dt_a:
        return dt_a.astimezone(timezone.utc).isoformat()
    if dt_b:
        return dt_b.astimezone(timezone.utc).isoformat()
    return None


def latest_timestamp_iso(ts_a: Optional[str], ts_b: Optional[str]) -> Optional[str]:
    dt_a = parse_iso(ts_a)
    dt_b = parse_iso(ts_b)
    if dt_a and dt_b:
        newer = dt_a if dt_a > dt_b else dt_b
        return newer.astimezone(timezone.utc).isoformat()
    if dt_a:
        return dt_a.astimezone(timezone.utc).isoformat()
    if dt_b:
        return dt_b.astimezone(timezone.utc).isoformat()
    return None


def to_float(val: object) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _flows_match(a: object, b: object, tolerance: float = 1e-6) -> bool:
    """Return True when two flow values are effectively equal."""
    fa = to_float(a)
    fb = to_float(b)
    if fa is None or fb is None:
        return False
    return abs(fa - fb) <= tolerance


def _flows_close(a: object, b: object, tolerance: float = 1e-3) -> bool:
    """True if both flows are finite and within tolerance."""
    fa = to_float(a)
    fb = to_float(b)
    if fa is None or fb is None:
        return False
    return abs(fa - fb) <= tolerance


def parse_retention_days(env: Dict[str, str]) -> Optional[float]:
    return to_float(env.get("DB_RETENTION_DAYS"))


def retention_check_days(env: Dict[str, str]) -> Optional[float]:
    return to_float(env.get("DB_RETENTION_CHECK_TIME"))


def retention_check_interval_seconds(env: Dict[str, str]) -> float:
    interval_days = retention_check_days(env)
    if interval_days is not None and interval_days > 0:
        return max(float(interval_days) * 24 * 60 * 60, 60.0)
    # Backward compatibility with earlier interval setting
    interval = to_float(env.get("DB_PRUNE_INTERVAL_SECONDS"))
    if interval is not None and interval > 0:
        return max(float(interval), 60.0)
    return float(DEFAULT_RETENTION_CHECK_SECONDS)


def should_run_prune(marker_path: Path, interval_seconds: float, now: datetime) -> bool:
    if not marker_path.exists():
        return True
    try:
        raw = marker_path.read_text().strip()
        last = datetime.fromisoformat(raw) if raw else None
    except Exception:
        return True
    if last is None:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last) >= timedelta(seconds=interval_seconds)


def prune_table_if_exists(
    conn: sqlite3.Connection, table: str, ts_column: str, cutoff_iso: str
) -> int:
    if not table_exists(conn, table):
        return 0
    with conn:
        cur = conn.execute(f"DELETE FROM {table} WHERE {ts_column} < ?", (cutoff_iso,))
    return cur.rowcount if cur.rowcount is not None else 0


def prune_server_databases(
    env: Dict[str, str],
    tank_conn: sqlite3.Connection,
    pump_conn: sqlite3.Connection,
    vacuum_conn: sqlite3.Connection,
    stack_conn: sqlite3.Connection,
    evap_conn: sqlite3.Connection,
) -> None:
    retention_days = parse_retention_days(env)
    if retention_days is None or retention_days <= 0:
        return
    interval_days = retention_check_days(env) or 0.0
    interval_seconds = retention_check_interval_seconds(env)
    now = datetime.now(timezone.utc)
    if interval_days >= 1.0:
        # Run only during a quiet window (2 AM EST)
        est_now = now.astimezone(EST_TZ)
        if est_now.hour != 2:
            return
    log_dir = repo_path_from_config(env.get("LOG_DIR", "data/logs"))
    marker_path = log_dir / PRUNE_MARKER_FILENAME
    if not should_run_prune(marker_path, interval_seconds, now):
        return
    log_dir.mkdir(parents=True, exist_ok=True)
    cutoff = (now - timedelta(days=retention_days)).isoformat()
    deletions = {
        "tank": prune_table_if_exists(tank_conn, "tank_readings", "source_timestamp", cutoff),
        "pump": prune_table_if_exists(pump_conn, "pump_events", "source_timestamp", cutoff),
        "vacuum": prune_table_if_exists(vacuum_conn, "vacuum_readings", "source_timestamp", cutoff),
        "stack": prune_table_if_exists(stack_conn, "stack_temperatures", "source_timestamp", cutoff),
        "evap": prune_table_if_exists(evap_conn, "evaporator_flow", "sample_timestamp", cutoff),
    }
    try:
        marker_path.write_text(now.isoformat())
    except Exception:
        pass
    total_deleted = sum(deletions.values())
    if total_deleted:
        print(f"Pruned {total_deleted} rows older than {retention_days} days")
        for label, conn in (
            ("tank", tank_conn),
            ("pump", pump_conn),
            ("vacuum", vacuum_conn),
            ("stack", stack_conn),
            ("evap", evap_conn),
        ):
            if deletions.get(label, 0) <= 0:
                continue
            try:
                conn.execute("VACUUM")
            except Exception as exc:
                print(f"VACUUM failed for {label} DB: {exc}")


def resolve_emptying_threshold(env: Dict[str, str]) -> float:
    for key in ("tanks_emptying_threshold", "TANKS_EMPTYING_THRESHOLD"):
        val = env.get(key)
        numeric = to_float(val) if val is not None else None
        if numeric is not None:
            return float(numeric)
    return float(DEFAULT_EMPTYING_THRESHOLD)


def ensure_evap_tables(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS evaporator_flow (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sample_timestamp TEXT NOT NULL,
                draw_off_tank TEXT,
                pump_in_tank TEXT,
                draw_off_flow_gph REAL,
                pump_in_flow_gph REAL,
                pump_flow_gph REAL,
                brookside_flow_gph REAL,
                roadside_flow_gph REAL,
                evaporator_flow_gph REAL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_evap_sample_timestamp
            ON evaporator_flow(sample_timestamp)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS plot_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                y_axis_min REAL NOT NULL,
                y_axis_max REAL NOT NULL,
                window_sec INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        cur = conn.execute("SELECT y_axis_min, y_axis_max, window_sec FROM plot_settings WHERE id=1")
        if cur.fetchone() is None:
            y_min, y_max, window_sec = DEFAULT_PLOT_SETTINGS
            conn.execute(
                """
                INSERT INTO plot_settings (id, y_axis_min, y_axis_max, window_sec, updated_at)
                VALUES (1, ?, ?, ?, ?)
                """,
                (y_min, y_max, window_sec, iso_now()),
            )


def load_plot_settings(conn: sqlite3.Connection) -> Tuple[float, float, int]:
    cur = conn.execute("SELECT y_axis_min, y_axis_max, window_sec FROM plot_settings WHERE id=1")
    row = cur.fetchone()
    if row:
        return float(row["y_axis_min"]), float(row["y_axis_max"]), int(row["window_sec"])
    return DEFAULT_PLOT_SETTINGS


def latest_evap_row(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    cur = conn.execute(
        "SELECT * FROM evaporator_flow ORDER BY sample_timestamp DESC, id DESC LIMIT 1"
    )
    return cur.fetchone()


def pick_draw_off(
    brookside_flow: Optional[float],
    roadside_flow: Optional[float],
    prev_draw_off: Optional[str],
    emptying_threshold: float,
) -> str:
    candidates = []
    if brookside_flow is not None and brookside_flow < emptying_threshold:
        candidates.append(("brookside", brookside_flow))
    if roadside_flow is not None and roadside_flow < emptying_threshold:
        candidates.append(("roadside", roadside_flow))
    if not candidates:
        return NO_TANK
    if len(candidates) == 2 and candidates[0][1] == candidates[1][1]:
        return prev_draw_off or NO_TANK
    candidates.sort(key=lambda item: item[1])  # most negative first
    return candidates[0][0]


def within_tolerance(value: float, target: float, tolerance_fraction: float) -> bool:
    if target == 0:
        return False
    return abs(value - target) <= abs(target) * tolerance_fraction


def pick_pump_in(
    draw_off: str,
    pump_flow: Optional[float],
    other_flow: Optional[float],
    prev_pump_in: Optional[str],
) -> str:
    if pump_flow is None:
        return NO_TANK
    if pump_flow < PUMP_LOW_THRESHOLD:
        return NO_TANK
    if draw_off not in {"brookside", "roadside"}:
        return NO_TANK
    if other_flow is None:
        return prev_pump_in or NO_TANK
    if other_flow > ZERO_FLOW_TOLERANCE:
        if within_tolerance(other_flow, pump_flow, PUMP_MATCH_TOLERANCE):
            return "roadside" if draw_off == "brookside" else "brookside"
    if abs(other_flow) <= ZERO_FLOW_TOLERANCE:
        return draw_off
    if other_flow < -ZERO_FLOW_TOLERANCE:
        return prev_pump_in or NO_TANK
    return NO_TANK


def compute_evaporator_flow(
    draw_off: str,
    pump_in: str,
    brookside_flow: Optional[float],
    roadside_flow: Optional[float],
    pump_flow: Optional[float],
    emptying_threshold: float,
) -> Optional[float]:
    if brookside_flow is None and roadside_flow is None:
        return None
    most_negative_flow = None
    for flow in (brookside_flow, roadside_flow):
        if flow is not None and flow < emptying_threshold:
            if most_negative_flow is None or flow < most_negative_flow:
                most_negative_flow = flow
    if most_negative_flow is None:
        return 0.0
    total_flow = abs(most_negative_flow)
    if pump_in == draw_off and pump_flow is not None:
        total_flow += max(pump_flow, 0.0)
    return total_flow


def is_duplicate_evap(prev_row: Optional[sqlite3.Row], record: Dict[str, object]) -> bool:
    """Avoid inserting identical evaporator rows repeatedly."""
    if not prev_row:
        return False
    same_draw = (prev_row["draw_off_tank"] or NO_TANK) == (record.get("draw_off_tank") or NO_TANK)
    same_pump = (prev_row["pump_in_tank"] or NO_TANK) == (record.get("pump_in_tank") or NO_TANK)
    same_evap = _flows_close(prev_row["evaporator_flow_gph"], record.get("evaporator_flow_gph"))
    same_draw_flow = _flows_close(prev_row["draw_off_flow_gph"], record.get("draw_off_flow_gph"))
    same_pump_flow = _flows_close(prev_row["pump_in_flow_gph"], record.get("pump_in_flow_gph"))
    return same_draw and same_pump and same_evap and same_draw_flow and same_pump_flow


def build_evap_record(
    tank_rows: Dict[str, sqlite3.Row],
    pump_row: Optional[sqlite3.Row],
    prev_row: Optional[sqlite3.Row],
    emptying_threshold: float,
) -> Optional[Dict[str, object]]:
    brook_row = tank_rows.get("brookside")
    road_row = tank_rows.get("roadside")
    brook_flow = to_float(brook_row["flow_gph"]) if brook_row else None
    road_flow = to_float(road_row["flow_gph"]) if road_row else None
    pump_flow = to_float(pump_row["gallons_per_hour"]) if pump_row else None

    draw_off_prev = (prev_row["draw_off_tank"] if prev_row else None) or NO_TANK
    pump_in_prev = (prev_row["pump_in_tank"] if prev_row else None) or NO_TANK

    draw_off = pick_draw_off(brook_flow, road_flow, draw_off_prev, emptying_threshold)
    other_flow = None
    if draw_off == "brookside":
        other_flow = road_flow
    elif draw_off == "roadside":
        other_flow = brook_flow
    pump_in = pick_pump_in(draw_off, pump_flow, other_flow, pump_in_prev)
    draw_off_flow = brook_flow if draw_off == "brookside" else road_flow if draw_off == "roadside" else None
    if pump_in == draw_off:
        pump_in_flow = draw_off_flow
    elif pump_in == "brookside":
        pump_in_flow = brook_flow
    elif pump_in == "roadside":
        pump_in_flow = road_flow
    else:
        pump_in_flow = None

    sample_ts = latest_timestamp_iso(
        brook_row["source_timestamp"] if brook_row else None,
        road_row["source_timestamp"] if road_row else None,
    )
    if sample_ts is None:
        return None
    prev_sample_ts = parse_iso(prev_row["sample_timestamp"]) if prev_row else None
    current_ts_dt = parse_iso(sample_ts)
    if prev_sample_ts and current_ts_dt and current_ts_dt <= prev_sample_ts:
        return None

    evap_flow = compute_evaporator_flow(
        draw_off, pump_in, brook_flow, road_flow, pump_flow, emptying_threshold
    )
    if evap_flow is None:
        return None
    record = {
        "sample_timestamp": sample_ts,
        "draw_off_tank": draw_off,
        "pump_in_tank": pump_in,
        "draw_off_flow_gph": draw_off_flow,
        "pump_in_flow_gph": pump_in_flow,
        "pump_flow_gph": pump_flow,
        "brookside_flow_gph": brook_flow,
        "roadside_flow_gph": road_flow,
        "evaporator_flow_gph": evap_flow,
        "created_at": iso_now(),
    }
    if is_duplicate_evap(prev_row, record):
        return None
    return record


def insert_evap_record(conn: sqlite3.Connection, record: Dict[str, object]) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO evaporator_flow (
                sample_timestamp, draw_off_tank, pump_in_tank, draw_off_flow_gph,
                pump_in_flow_gph, pump_flow_gph, brookside_flow_gph, roadside_flow_gph,
                evaporator_flow_gph, created_at
            ) VALUES (
                :sample_timestamp, :draw_off_tank, :pump_in_tank, :draw_off_flow_gph,
                :pump_in_flow_gph, :pump_flow_gph, :brookside_flow_gph, :roadside_flow_gph,
                :evaporator_flow_gph, :created_at
            )
            ON CONFLICT(sample_timestamp) DO UPDATE SET
                draw_off_tank=excluded.draw_off_tank,
                pump_in_tank=excluded.pump_in_tank,
                draw_off_flow_gph=excluded.draw_off_flow_gph,
                pump_in_flow_gph=excluded.pump_in_flow_gph,
                pump_flow_gph=excluded.pump_flow_gph,
                brookside_flow_gph=excluded.brookside_flow_gph,
                roadside_flow_gph=excluded.roadside_flow_gph,
                evaporator_flow_gph=excluded.evaporator_flow_gph,
                created_at=excluded.created_at
            """,
            record,
        )


def build_evap_status_payload(
    record: Dict[str, object], generated_at: str, settings: Tuple[float, float, int]
) -> Dict[str, object]:
    y_min, y_max, window_sec = settings
    return {
        "generated_at": generated_at,
        "sample_timestamp": record.get("sample_timestamp"),
        "draw_off_tank": record.get("draw_off_tank"),
        "pump_in_tank": record.get("pump_in_tank"),
        "draw_off_flow_gph": record.get("draw_off_flow_gph"),
        "pump_in_flow_gph": record.get("pump_in_flow_gph"),
        "pump_flow_gph": record.get("pump_flow_gph"),
        "brookside_flow_gph": record.get("brookside_flow_gph"),
        "roadside_flow_gph": record.get("roadside_flow_gph"),
        "evaporator_flow_gph": record.get("evaporator_flow_gph"),
        "plot_settings": {
            "y_axis_min": y_min,
            "y_axis_max": y_max,
            "window_sec": window_sec,
        },
    }


def main() -> None:
    env = load_role(
        "server",
        required=[
            "TANK_DB_PATH",
            "PUMP_DB_PATH",
            "STATUS_JSON_PATH",
            "TANK_CAPACITY_BROOKSIDE",
            "TANK_CAPACITY_ROADSIDE",
        ],
    )
    tank_capacity = {
        "brookside": float(env["TANK_CAPACITY_BROOKSIDE"]),
        "roadside": float(env["TANK_CAPACITY_ROADSIDE"]),
    }
    emptying_threshold = resolve_emptying_threshold(env)

    tank_conn = open_db(repo_path_from_config(env["TANK_DB_PATH"]))
    pump_conn = open_db(repo_path_from_config(env["PUMP_DB_PATH"]))
    evap_path_cfg = env.get("EVAPORATOR_DB_PATH", "data/evaporator.db")
    evap_conn = open_db(repo_path_from_config(evap_path_cfg))
    vacuum_db_path = repo_path_from_config(env.get("VACUUM_DB_PATH", env["PUMP_DB_PATH"]))
    vacuum_conn = open_db(vacuum_db_path)
    stack_db_path = repo_path_from_config(env.get("STACK_TEMP_DB_PATH", env.get("VACUUM_DB_PATH", env["PUMP_DB_PATH"])))
    stack_conn = vacuum_conn if stack_db_path == vacuum_db_path else open_db(stack_db_path)
    ensure_evap_tables(evap_conn)
    prune_server_databases(env, tank_conn, pump_conn, vacuum_conn, stack_conn, evap_conn)
    plot_settings = load_plot_settings(evap_conn)

    status_base = repo_path_from_config(env["STATUS_JSON_PATH"]).parent

    timestamp = iso_now()
    tank_rows = get_latest_tank_rows(tank_conn)
    for tank_id, row in tank_rows.items():
        max_volume = row["max_volume_gal"] or tank_capacity.get(tank_id)
        if max_volume is None:
            max_volume = tank_capacity.get(tank_id)
        percent = row["level_percent"]
        if percent is None:
            percent = calc_percent(row["volume_gal"], max_volume)
        payload = {
            "generated_at": timestamp,
            "tank_id": tank_id,
            "volume_gal": row["volume_gal"],
            "max_volume_gal": max_volume,
            "level_percent": percent,
            "flow_gph": row["flow_gph"],
            "eta_full": row["eta_full"],
            "eta_empty": row["eta_empty"],
            "time_to_full_min": row["time_to_full_min"],
            "time_to_empty_min": row["time_to_empty_min"],
            "last_sample_timestamp": row["source_timestamp"],
            "last_received_at": row["received_at"],
        }
        tank_status_path = status_base / f"status_{tank_id}.json"
        if should_update_status(tank_status_path, payload["last_sample_timestamp"]):
            atomic_write(tank_status_path, payload)

    pump_payload = None
    pump_row = get_latest_pump_row(pump_conn)
    pump_gph_row = get_latest_pump_gph(pump_conn)
    if pump_row:
        gph = pump_row["gallons_per_hour"]
        if gph is None and pump_gph_row is not None:
            gph = pump_gph_row["gallons_per_hour"]
        event_type = pump_row["event_type"]
        pump_status = "Pumping"
        pump_fatal = False
        if isinstance(event_type, str):
            lowered = event_type.lower()
            if lowered == "pump stop":
                pump_status = "Not pumping"
            elif lowered == "manual pump start":
                pump_status = "Manual pumping"
            elif lowered == "fatal error":
                pump_status = "FATAL ERROR"
                pump_fatal = True
        pump_row = dict(pump_row)
        pump_row["gallons_per_hour"] = gph
        pump_payload = {
            "generated_at": timestamp,
            "event_type": event_type,
            "pump_run_time_s": pump_row["pump_run_time_s"],
            "pump_interval_s": pump_row["pump_interval_s"],
            "gallons_per_hour": gph,
            "last_event_timestamp": pump_row["source_timestamp"],
            "last_received_at": pump_row["received_at"],
            "pump_status": pump_status,
            "pump_fatal": pump_fatal,
        }
        pump_status_path = status_base / "status_pump.json"
        if should_update_status(pump_status_path, pump_payload["last_event_timestamp"]):
            atomic_write(pump_status_path, pump_payload)

    vac_row = get_latest_vacuum_row(vacuum_conn)
    if vac_row:
        vacuum_payload = {
            "generated_at": timestamp,
            "reading_inhg": vac_row["reading_inhg"],
            "source_timestamp": vac_row["source_timestamp"],
            "last_received_at": vac_row["received_at"],
        }
        vac_path = status_base / "status_vacuum.json"
        if should_update_status(vac_path, vacuum_payload["source_timestamp"]):
            atomic_write(vac_path, vacuum_payload)

    stack_row = get_latest_stack_temp_row(stack_conn)
    if stack_row:
        stack_payload = {
            "generated_at": timestamp,
            "stack_temp_f": stack_row["stack_temp_f"],
            "ambient_temp_f": stack_row["ambient_temp_f"],
            "source_timestamp": stack_row["source_timestamp"],
            "last_received_at": stack_row["received_at"],
        }
        stack_path = status_base / "status_stack.json"
        if should_update_status(stack_path, stack_payload["source_timestamp"]):
            atomic_write(stack_path, stack_payload)

    evap_prev = latest_evap_row(evap_conn)
    evap_record = build_evap_record(tank_rows, pump_row, evap_prev, emptying_threshold)
    if evap_record:
        insert_evap_record(evap_conn, evap_record)
        evap_prev = evap_record
        evap_payload = build_evap_status_payload(evap_record, timestamp, plot_settings)
    elif evap_prev:
        # No change worth recording; reuse previous row for status freshness.
        evap_payload = build_evap_status_payload(dict(evap_prev), timestamp, plot_settings)
    else:
        # Ensure a placeholder exists so clients don't 404 while waiting for data.
        evap_payload = build_evap_status_payload(
            {
                "sample_timestamp": None,
                "draw_off_tank": "---",
                "pump_in_tank": "---",
                "draw_off_flow_gph": None,
                "pump_in_flow_gph": None,
                "pump_flow_gph": None,
                "brookside_flow_gph": None,
                "roadside_flow_gph": None,
                "evaporator_flow_gph": None,
            },
            timestamp,
            plot_settings,
        )
    atomic_write(status_base / "status_evaporator.json", evap_payload)

    monitor_payload = {
        "generated_at": timestamp,
        "tank_monitor_last_received_at": get_latest_monitor_ts(tank_conn, "tank"),
        "pump_monitor_last_received_at": get_latest_monitor_ts(pump_conn, "pump"),
        "pump_fatal": pump_payload["pump_fatal"] if pump_row else None,
    }
    atomic_write(status_base / "status_monitor.json", monitor_payload)


if __name__ == "__main__":
    main()
