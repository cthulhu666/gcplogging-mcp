"""TOML config loading and validation."""

from __future__ import annotations

import dataclasses
import os
import stat
import tomllib
from typing import Any


@dataclasses.dataclass(frozen=True)
class ProjectConfig:
    name: str
    project_id: str
    description: str = ""
    credentials_file: str | None = None
    # Page fetches per minute. Cloud Logging's default read quota is 60/min per project;
    # None falls back to logs.DEFAULT_READ_RPM.
    read_requests_per_minute: int | None = None


def config_path() -> str:
    return os.path.expanduser(
        os.environ.get("GCPLOGGING_MCP_CONFIG", "~/.config/gcplogging-mcp/config.toml")
    )


def load_projects() -> dict[str, ProjectConfig]:
    path = config_path()

    if not os.path.exists(path):
        raise RuntimeError(f"Config file not found: {path}")

    mode = stat.S_IMODE(os.stat(path).st_mode)
    if mode & 0o077:
        raise RuntimeError(
            f"Config file {path} is group- or world-readable. Run: chmod 600 {path}"
        )

    with open(path, "rb") as f:
        data = tomllib.load(f)

    projects: dict[str, Any] = data.get("projects", {})
    if not projects:
        raise RuntimeError(f"No [projects] entries found in {path}")

    settings: dict[str, Any] = data.get("settings", {})
    global_credentials = settings.get("credentials_file")
    global_rpm = settings.get("read_requests_per_minute")

    result: dict[str, ProjectConfig] = {}
    for name, entry in projects.items():
        if "project_id" not in entry:
            raise RuntimeError(f"[projects.{name}] is missing required field 'project_id'")
        credentials_file = entry.get("credentials_file") or global_credentials
        rpm = entry.get("read_requests_per_minute", global_rpm)
        if rpm is not None and (not isinstance(rpm, int) or rpm < 1):
            raise RuntimeError(
                f"[projects.{name}] read_requests_per_minute must be a positive integer"
            )
        result[name] = ProjectConfig(
            name=name,
            project_id=entry["project_id"],
            description=entry.get("description", ""),
            credentials_file=os.path.expanduser(credentials_file) if credentials_file else None,
            read_requests_per_minute=rpm,
        )

    return result
