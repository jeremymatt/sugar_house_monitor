"""
Generate per-component status JSON files from the latest tank + pump readings.

This script is triggered automatically by the ingest PHP endpoints.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
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
            SELECT tank_id, MAX(received_at) AS max_received
            FROM tank_readings
            GROUP BY tank_id
        ) latest
            ON latest.tank_id = tr.tank_id AND latest.max_received = tr.received_at
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

NEG_FLOW_THRESHOLD = -2.5
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


def to_float(val: object) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


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
) -> str:
    candidates = []
    if brookside_flow is not None and brookside_flow < NEG_FLOW_THRESHOLD:
        candidates.append(("brookside", brookside_flow))
    if roadside_flow is not None and roadside_flow < NEG_FLOW_THRESHOLD:
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
    draw_off_flow: Optional[float],
    brookside_flow: Optional[float],
    roadside_flow: Optional[float],
    pump_flow: Optional[float],
) -> Optional[float]:
    if brookside_flow is None and roadside_flow is None:
        return None
    if (
        (brookside_flow is None or brookside_flow >= NEG_FLOW_THRESHOLD)
        and (roadside_flow is None or roadside_flow >= NEG_FLOW_THRESHOLD)
    ):
        return 0.0
    if draw_off not in {"brookside", "roadside"}:
        return 0.0
    if draw_off_flow is None:
        return None
    if pump_in == draw_off:
        return abs(draw_off_flow) + max(pump_flow or 0.0, 0.0)
    return abs(draw_off_flow)


def build_evap_record(
    tank_rows: Dict[str, sqlite3.Row],
    pump_row: Optional[sqlite3.Row],
    prev_row: Optional[sqlite3.Row],
) -> Optional[Dict[str, object]]:
    brook_row = tank_rows.get("brookside")
    road_row = tank_rows.get("roadside")
    brook_flow = to_float(brook_row["flow_gph"]) if brook_row else None
    road_flow = to_float(road_row["flow_gph"]) if road_row else None
    pump_flow = to_float(pump_row["gallons_per_hour"]) if pump_row else None

    draw_off_prev = (prev_row["draw_off_tank"] if prev_row else None) or NO_TANK
    pump_in_prev = (prev_row["pump_in_tank"] if prev_row else None) or NO_TANK

    draw_off = pick_draw_off(brook_flow, road_flow, draw_off_prev)
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

    sample_ts = average_timestamp_iso(
        brook_row["source_timestamp"] if brook_row else None,
        road_row["source_timestamp"] if road_row else None,
    )
    if sample_ts is None:
        return None

    evap_flow = compute_evaporator_flow(
        draw_off, pump_in, draw_off_flow, brook_flow, road_flow, pump_flow
    )

    return {
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

    tank_conn = open_db(repo_path_from_config(env["TANK_DB_PATH"]))
    pump_conn = open_db(repo_path_from_config(env["PUMP_DB_PATH"]))
    evap_path_cfg = env.get("EVAPORATOR_DB_PATH", "data/evaporator.db")
    evap_conn = open_db(repo_path_from_config(evap_path_cfg))
    vacuum_db_path = repo_path_from_config(env.get("VACUUM_DB_PATH", env["PUMP_DB_PATH"]))
    vacuum_conn = open_db(vacuum_db_path)
    ensure_evap_tables(evap_conn)
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
        atomic_write(status_base / f"status_{tank_id}.json", payload)

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
        atomic_write(status_base / "status_pump.json", pump_payload)

    vac_row = get_latest_vacuum_row(vacuum_conn)
    if vac_row:
        vacuum_payload = {
            "generated_at": timestamp,
            "reading_inhg": vac_row["reading_inhg"],
            "source_timestamp": vac_row["source_timestamp"],
            "last_received_at": vac_row["received_at"],
        }
        atomic_write(status_base / "status_vacuum.json", vacuum_payload)

    evap_prev = latest_evap_row(evap_conn)
    evap_record = build_evap_record(tank_rows, pump_row, evap_prev)
    if evap_record:
        insert_evap_record(evap_conn, evap_record)
        evap_payload = build_evap_status_payload(evap_record, timestamp, plot_settings)
        atomic_write(status_base / "status_evaporator.json", evap_payload)
    else:
        # Ensure a placeholder exists so clients don't 404 while waiting for data.
        placeholder = build_evap_status_payload(
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
        atomic_write(status_base / "status_evaporator.json", placeholder)

    monitor_payload = {
        "generated_at": timestamp,
        "tank_monitor_last_received_at": get_latest_monitor_ts(tank_conn, "tank"),
        "pump_monitor_last_received_at": get_latest_monitor_ts(pump_conn, "pump"),
        "pump_fatal": pump_payload["pump_fatal"] if pump_row else None,
    }
    atomic_write(status_base / "status_monitor.json", monitor_payload)


if __name__ == "__main__":
    main()
