"""
Generate web/data/status.json from the latest tank + pump readings.

This script is triggered automatically by the ingest PHP endpoints.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

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


def calc_percent(volume: Optional[float], capacity: Optional[float]) -> Optional[float]:
    if volume is None or capacity in (None, 0):
        return None
    return max(0.0, min(100.0, (float(volume) / float(capacity)) * 100.0))


def atomic_write(path: Path, payload: Dict) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path.write_text(json.dumps(payload, indent=2))
    tmp_path.replace(path)


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

    tanks = {}
    for tank_id, row in get_latest_tank_rows(tank_conn).items():
        cap = tank_capacity.get(tank_id)
        volume = row["volume_gal"]
        tanks[tank_id] = {
            "tank_id": tank_id,
            "volume_gal": volume,
            "capacity_gal": cap,
            "level_percent": calc_percent(volume, cap),
            "flow_gph": row["flow_gph"],
            "eta_full": row["eta_full"],
            "eta_empty": row["eta_empty"],
            "last_sample_timestamp": row["source_timestamp"],
            "last_received_at": row["received_at"],
        }

    pump_row = get_latest_pump_row(pump_conn)
    pump_section = None
    if pump_row:
        pump_section = {
            "event_type": pump_row["event_type"],
            "pump_run_time_s": pump_row["pump_run_time_s"],
            "pump_interval_s": pump_row["pump_interval_s"],
            "gallons_per_hour": pump_row["gallons_per_hour"],
            "last_event_timestamp": pump_row["source_timestamp"],
            "last_received_at": pump_row["received_at"],
        }

    payload = {
        "generated_at": iso_now(),
        "tanks": tanks,
        "pump": pump_section,
    }

    status_path = repo_path_from_config(env["STATUS_JSON_PATH"])
    atomic_write(status_path, payload)


if __name__ == "__main__":
    main()
