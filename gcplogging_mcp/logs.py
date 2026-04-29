"""Google Cloud Logging API calls and file writing."""

from __future__ import annotations

import datetime
import json
import os
import tempfile
from typing import Any

from gcplogging_mcp.config import ProjectConfig


class _Encoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        if isinstance(obj, (datetime.date, datetime.datetime)):
            return obj.isoformat()
        return super().default(obj)


def _make_client(cfg: ProjectConfig) -> Any:
    import google.cloud.logging

    if cfg.credentials_file:
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(
            cfg.credentials_file,
            scopes=["https://www.googleapis.com/auth/logging.read"],
        )
        return google.cloud.logging.Client(project=cfg.project_id, credentials=creds)

    return google.cloud.logging.Client(project=cfg.project_id)


def _entry_to_dict(entry: Any) -> dict[str, Any]:
    payload: Any
    if hasattr(entry, "payload") and isinstance(entry.payload, dict):
        payload = entry.payload
    elif hasattr(entry, "payload"):
        payload = str(entry.payload)
    else:
        payload = None

    return {
        "timestamp": entry.timestamp.isoformat() if entry.timestamp else None,
        "severity": entry.severity,
        "log_name": entry.log_name,
        "resource": {
            "type": entry.resource.type if entry.resource else None,
            "labels": dict(entry.resource.labels) if entry.resource else {},
        },
        "payload": payload,
        "trace": getattr(entry, "trace", None),
        "labels": dict(entry.labels) if entry.labels else {},
        "insert_id": getattr(entry, "insert_id", None),
    }


def list_logs(cfg: ProjectConfig) -> dict[str, Any]:
    from google.cloud.logging_v2.services.logging_service_v2 import LoggingServiceV2Client

    if cfg.credentials_file:
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_file(cfg.credentials_file)
        low = LoggingServiceV2Client(credentials=creds)
    else:
        low = LoggingServiceV2Client()

    log_names = list(low.list_logs(parent=f"projects/{cfg.project_id}"))
    return {"log_names": log_names}


def download_logs(
    cfg: ProjectConfig,
    filter_str: str,
    start_time: str,
    end_time: str | None,
    output_file: str | None,
) -> dict[str, Any]:
    if end_time is None:
        end_time = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    full_filter = f'{filter_str} AND timestamp>="{start_time}" AND timestamp<"{end_time}"'

    if output_file is None:
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
        output_file = os.path.join(tempfile.gettempdir(), f"gcplogging-{cfg.name}-{ts}.jsonl")

    client = _make_client(cfg)
    count = 0
    with open(output_file, "w") as f:
        for entry in client.list_entries(
            resource_names=[f"projects/{cfg.project_id}"],
            filter_=full_filter,
            order_by="timestamp asc",
            page_size=1000,
        ):
            f.write(json.dumps(_entry_to_dict(entry), cls=_Encoder) + "\n")
            count += 1

    return {
        "file": output_file,
        "entry_count": count,
        "bytes_written": os.path.getsize(output_file),
        "filter": full_filter,
    }
