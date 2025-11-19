"""
Generate per-component status JSON files from the latest tank + pump readings.

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

    status_base = repo_path_from_config(env["STATUS_JSON_PATH"]).parent

    timestamp = iso_now()
    for tank_id, row in get_latest_tank_rows(tank_conn).items():
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

    pump_row = get_latest_pump_row(pump_conn)
    if pump_row:
        pump_payload = {
            "generated_at": timestamp,
            "event_type": pump_row["event_type"],
            "pump_run_time_s": pump_row["pump_run_time_s"],
            "pump_interval_s": pump_row["pump_interval_s"],
            "gallons_per_hour": pump_row["gallons_per_hour"],
            "last_event_timestamp": pump_row["source_timestamp"],
            "last_received_at": pump_row["received_at"],
        }
        atomic_write(status_base / "status_pump.json", pump_payload)


if __name__ == "__main__":
    main()
