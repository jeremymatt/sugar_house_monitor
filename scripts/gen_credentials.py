"""
Interactive helper that bootstraps config/*.env files.

The script generates a shared API key and writes populated env files for the
server, Tank Pi, and Pump Pi using the templates in config/example/.
"""
from __future__ import annotations

import secrets
import textwrap
from pathlib import Path

from config_loader import REPO_ROOT, get_config_dir


def prompt(message: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{message}{suffix}: ").strip()
    if not value and default is not None:
        return default
    return value


def confirm_overwrite(path: Path) -> bool:
    if not path.exists():
        return True
    answer = input(f"{path} already exists. Overwrite? [y/N]: ").strip().lower()
    return answer in {"y", "yes"}


def write_env(path: Path, lines: str) -> None:
    path.write_text(textwrap.dedent(lines).strip() + "\n")
    print(f"Wrote {path}")


def main() -> None:
    config_dir = get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)

    default_base = "https://example.com/sugar_house_monitor"
    api_base = prompt("Public base URL for the API (no trailing slash)", default_base)
    api_base = api_base.rstrip("/")
    api_key = secrets.token_urlsafe(32)

    server_env = textwrap.dedent(
        f"""
        ROLE=server
        API_KEY={api_key}
        TANK_DB_PATH=data/tank_server.db
        PUMP_DB_PATH=data/pump_server.db
        STATUS_JSON_PATH=web/data/status.json
        EXPORT_DIR=data/exports
        LOG_DIR=data/logs
        TANK_CAPACITY_BROOKSIDE=300.0
        TANK_CAPACITY_ROADSIDE=300.0
        TIMEZONE=UTC
        """
    ).strip()

    tank_env = textwrap.dedent(
        f"""
        ROLE=tank_pi
        API_BASE_URL={api_base}/api
        API_KEY={api_key}
        DB_PATH=data/tank_pi.db
        UPLOAD_BATCH_SIZE=10
        UPLOAD_INTERVAL_SECONDS=60
        DEBUG_TANK=false
        DEBUG_RELEASER=false
        SYNTHETIC_CLOCK_MULTIPLIER=2.0
        BROOKSIDE_CSV=real_data/brookside.csv
        ROADSIDE_CSV=real_data/roadside.csv
        PUMP_EVENTS_CSV=real_data/pump_times.csv
        STATUS_JSON_PATH=web/data/status.json
        """
    ).strip()

    pump_env = textwrap.dedent(
        f"""
        ROLE=pump_pi
        API_BASE_URL={api_base}/api
        API_KEY={api_key}
        DB_PATH=data/pump_pi.db
        UPLOAD_BATCH_SIZE=10
        UPLOAD_INTERVAL_SECONDS=60
        """
    ).strip()

    targets = {
        "server.env": server_env,
        "tank_pi.env": tank_env,
        "pump_pi.env": pump_env,
    }

    print(f"\nConfig root: {config_dir}")
    for filename, contents in targets.items():
        dest = config_dir / filename
        if confirm_overwrite(dest):
            write_env(dest, contents)
        else:
            print(f"Skipped {dest}")

    print("\nDone. Copy each env file to its destination device as needed.")


if __name__ == "__main__":
    main()
