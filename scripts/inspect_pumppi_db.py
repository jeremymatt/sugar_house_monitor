#!/usr/bin/env python3
import sqlite3
from pathlib import Path

this_filepath = Path(__file__)
repo_root = this_filepath.parent.parent

DB_PATH = repo_root / 'data' / 'pump_pi.db'


def summarize_table(conn, table):
    cur = conn.execute(f"""
        SELECT COUNT(*) AS rows,
               MIN(source_timestamp) AS first_ts,
               MAX(source_timestamp) AS last_ts
        FROM {table}
    """)
    row = cur.fetchone()
    print(f"{table}: rows={row['rows']} first={row['first_ts']} last={row['last_ts']}")

def ack_breakdown(conn, table):
    cur = conn.execute(f"""
        SELECT sent_to_server, acked_by_server, COUNT(*) AS rows
        FROM {table}
        GROUP BY sent_to_server, acked_by_server
    """)
    print(f"{table} (by ack status):")
    for sent, acked, count in cur.fetchall():
        print(f"  sent={sent} acked={acked} rows={count}")

def main():
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}")
        return
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    for table in ("pump_events", "vacuum_readings", "error_logs"):
        summarize_table(conn, table)
    print()
    for table in ("pump_events", "vacuum_readings"):
        ack_breakdown(conn, table)

if __name__ == "__main__":
    main()

