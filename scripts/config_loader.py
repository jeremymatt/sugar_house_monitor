"""
Utility helpers for loading repo-scoped .env files.

All services share a gitignored config directory that defaults to
<repo>/config unless SUGAR_CONFIG_DIR overrides it.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Iterable, Tuple


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_DIR = REPO_ROOT / "config"


def get_config_dir() -> Path:
    """Return the folder that holds *.env files."""
    override = os.environ.get("SUGAR_CONFIG_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return DEFAULT_CONFIG_DIR


def read_env_file(name: str) -> Dict[str, str]:
    """
    Load KEY=VALUE pairs from config/<name>.env.

    Lines starting with # are ignored. Empty lines are skipped. Values are
    returned as raw strings to keep parsing decisions near the caller.
    """
    config_dir = get_config_dir()
    path = config_dir / f"{name}.env"
    if not path.exists():
        raise FileNotFoundError(
            f"Config file {path} does not exist. "
            "Copy one of the templates in config/example/."
        )

    entries: Dict[str, str] = {}
    for line_num, raw_line in enumerate(path.read_text().splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"Invalid line in {path} (line {line_num}): {raw_line!r}")
        line = line.split('#', 1)[0].strip()
        key, value = line.split("=", 1)
        entries[key.strip()] = value.strip()
    return entries


def ensure_keys(env: Dict[str, str], keys: Iterable[str]) -> None:
    """Raise if any required keys are missing."""
    missing = [k for k in keys if k not in env or env[k] == ""]
    if missing:
        raise KeyError(f"Missing required config keys: {', '.join(missing)}")


def load_role(role: str, required: Iterable[str] | None = None) -> Dict[str, str]:
    """Shortcut for read_env + ensure_keys."""
    env = read_env_file(role)
    if required:
        ensure_keys(env, required)
    return env


def repo_path_from_config(path_value: str) -> Path:
    """
    Interpret path values that may be relative to the repo root.

    Accepts absolute paths or repo-relative strings such as ../data/foo.db.
    """
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = (REPO_ROOT / path).resolve()
    return path


__all__ = [
    "DEFAULT_CONFIG_DIR",
    "REPO_ROOT",
    "get_config_dir",
    "read_env_file",
    "load_role",
    "repo_path_from_config",
]
