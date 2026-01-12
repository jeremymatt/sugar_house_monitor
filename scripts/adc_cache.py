"""Helpers for sharing ADC samples between processes."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict

from config_loader import repo_path_from_config

DEFAULT_CACHE_NAME = "pump_adc_cache.json"


def resolve_cache_path(env: Dict[str, str]) -> Path:
    override = env.get("ADC_CACHE_PATH")
    if override:
        return repo_path_from_config(override)
    shm_path = Path("/dev/shm")
    if shm_path.exists() and os.access(shm_path, os.W_OK):
        return shm_path / DEFAULT_CACHE_NAME
    return repo_path_from_config(f"data/{DEFAULT_CACHE_NAME}")


def write_cache(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as fp:
        json.dump(payload, fp, separators=(",", ":"), sort_keys=False)
        fp.write("\n")
    os.replace(tmp_path, path)


def read_cache(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fp:
        return json.load(fp)


def cache_age_seconds(payload: Dict[str, Any]) -> float:
    monotonic = payload.get("monotonic")
    if monotonic is None:
        return float("inf")
    try:
        monotonic_value = float(monotonic)
    except (TypeError, ValueError):
        return float("inf")
    return max(0.0, time.monotonic() - monotonic_value)
