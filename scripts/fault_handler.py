"""Shared fault handler setup for pump services."""
from __future__ import annotations

import faulthandler
import os
import signal
from pathlib import Path
from typing import Optional

from config_loader import repo_path_from_config
from main_pump import LocalErrorWriter, PumpDatabase, iso_now


def setup_faulthandler(
    service_name: str,
    error_writer: Optional[LocalErrorWriter] = None,
    db: Optional[PumpDatabase] = None,
) -> Path:
    dump_dir = Path(os.environ.get("FAULT_DUMP_DIR", str(repo_path_from_config("data/fault_dumps"))))
    dump_dir.mkdir(parents=True, exist_ok=True)
    dump_path = dump_dir / f"{service_name}_fault.log"

    _ingest_previous_dump(dump_path, service_name, error_writer, db)

    dump_file = dump_path.open("a", encoding="utf-8", buffering=1)
    faulthandler.enable(file=dump_file, all_threads=True)
    faulthandler.register(signal.SIGUSR1, file=dump_file, all_threads=True)
    return dump_path


def _ingest_previous_dump(
    dump_path: Path,
    service_name: str,
    error_writer: Optional[LocalErrorWriter],
    db: Optional[PumpDatabase],
) -> None:
    try:
        if not dump_path.exists() or dump_path.stat().st_size == 0:
            return
        content = dump_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return

    message = f"Previous crash dump for {service_name}:\n{content}"
    if error_writer is not None:
        try:
            error_writer.append(message, source=service_name)
        except Exception:
            pass
    if db is not None:
        try:
            db.insert_error_log(
                {
                    "source": service_name,
                    "message": message,
                    "source_timestamp": iso_now(),
                }
            )
        except Exception:
            pass

    try:
        dump_path.write_text("")
    except Exception:
        pass
