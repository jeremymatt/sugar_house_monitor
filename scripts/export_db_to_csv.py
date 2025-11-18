"""
Export the server SQLite databases to CSV files on demand.
"""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

from config_loader import load_role, repo_path_from_config


def open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def export_table(conn: sqlite3.Connection, query: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", newline="") as fp:
        writer = None
        for row in conn.execute(query):
            if writer is None:
                writer = csv.DictWriter(fp, fieldnames=row.keys())
                writer.writeheader()
            writer.writerow(dict(row))
        if writer is None:
            # Ensure at least headers exist when table is empty.
            cursor = conn.execute(query)
            writer = csv.writer(fp)
            writer.writerow([col[0] for col in cursor.description or []])


def main() -> None:
    env = load_role(
        "server",
        required=["TANK_DB_PATH", "PUMP_DB_PATH", "EXPORT_DIR"],
    )
    tank_db = repo_path_from_config(env["TANK_DB_PATH"])
    pump_db = repo_path_from_config(env["PUMP_DB_PATH"])
    export_dir = repo_path_from_config(env["EXPORT_DIR"])

    export_table(
        open_db(tank_db),
        "SELECT * FROM tank_readings ORDER BY source_timestamp",
        export_dir / "tank_readings.csv",
    )
    export_table(
        open_db(pump_db),
        "SELECT * FROM pump_events ORDER BY source_timestamp",
        export_dir / "pump_events.csv",
    )
    print(f"Exports written to {export_dir}")


if __name__ == "__main__":
    main()
