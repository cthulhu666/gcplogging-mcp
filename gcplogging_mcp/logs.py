"""Google Cloud Logging API calls and file writing.

Cloud Logging enforces ReadRequestsPerMinutePerProject (60 by default). One page fetch
is one read request, so an unthrottled bulk download exhausts the quota in seconds and
dies mid-file. Everything below exists to keep a download inside that budget, and to make
a download that still fails resumable rather than lost.
"""

from __future__ import annotations

import datetime
import json
import os
import tempfile
import time
from collections import deque
from typing import Any, Iterator

from gcplogging_mcp.config import ProjectConfig

# Stay under the 60/min default: a page fetch issued just before the window rolls still
# counts against the old window server-side.
DEFAULT_READ_RPM = 50
MAX_PAGE_SIZE = 1000
MAX_QUOTA_RETRIES = 5


class _Encoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        if isinstance(obj, (datetime.date, datetime.datetime)):
            return obj.isoformat()
        return super().default(obj)


class _RateLimiter:
    """Sliding-window limiter over the last 60 seconds."""

    def __init__(self, rpm: int) -> None:
        self.rpm = max(1, rpm)
        self._times: deque[float] = deque()
        self.requests = 0
        self.wait_seconds = 0.0

    def acquire(self) -> None:
        while True:
            now = time.monotonic()
            while self._times and now - self._times[0] >= 60.0:
                self._times.popleft()
            if len(self._times) < self.rpm:
                self._times.append(now)
                self.requests += 1
                return
            sleep_for = 60.0 - (now - self._times[0]) + 0.05
            self.wait_seconds += sleep_for
            time.sleep(sleep_for)


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


def _is_quota_error(exc: BaseException) -> bool:
    try:
        from google.api_core import exceptions as gexc
    except ImportError:
        return "429" in str(exc)
    return isinstance(exc, (gexc.ResourceExhausted, gexc.TooManyRequests))


def _entries(
    client: Any,
    cfg: ProjectConfig,
    filter_str: str,
    limiter: _RateLimiter,
    max_results: int | None,
) -> Iterator[Any]:
    """Yield entries, spending one rate-limited token per page the generator fetches.

    ``Client.list_entries`` returns a flat generator that pulls a new page every
    ``page_size`` entries, so pacing per page means taking a token before the first
    entry and before each subsequent page boundary.
    """
    iterator = client.list_entries(
        resource_names=[f"projects/{cfg.project_id}"],
        filter_=filter_str,
        order_by="timestamp asc",
        page_size=MAX_PAGE_SIZE,
        max_results=max_results,
    )
    limiter.acquire()
    for seen, entry in enumerate(iterator, start=1):
        yield entry
        if seen % MAX_PAGE_SIZE == 0:
            limiter.acquire()


def download_logs(
    cfg: ProjectConfig,
    filter_str: str,
    start_time: str,
    end_time: str | None,
    output_file: str | None,
    max_entries: int | None = None,
    skip_insert_ids: list[str] | None = None,
) -> dict[str, Any]:
    if end_time is None:
        end_time = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    if output_file is None:
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d%H%M%S")
        output_file = os.path.join(tempfile.gettempdir(), f"gcplogging-{cfg.name}-{ts}.jsonl")

    full_filter = f'{filter_str} AND timestamp>="{start_time}" AND timestamp<"{end_time}"'
    limiter = _RateLimiter(cfg.read_requests_per_minute or DEFAULT_READ_RPM)
    client = _make_client(cfg)

    began = time.monotonic()
    count = 0
    cursor = start_time
    # Insert ids already written at exactly `cursor`. The resume window is inclusive of
    # `cursor` (an exclusive one would silently drop entries sharing that timestamp), so
    # this set is what keeps a resume from re-writing them.
    seen_at_cursor: set[str] = set(skip_insert_ids or ())
    quota_retries = 0
    status = "complete"
    note = ""
    truncated = False

    with open(output_file, "w") as f:
        while True:
            window = f'{filter_str} AND timestamp>="{cursor}" AND timestamp<"{end_time}"'
            # Budget the server-side cap for the entries this pass will skip as
            # already-written, otherwise max_results cuts the stream short and the run
            # looks complete when it is not.
            remaining = (
                None if max_entries is None else max_entries - count + len(seen_at_cursor)
            )
            try:
                for entry in _entries(client, cfg, window, limiter, remaining):
                    row = _entry_to_dict(entry)
                    ts_val = row["timestamp"]
                    insert_id = row["insert_id"]
                    if ts_val == cursor and insert_id and insert_id in seen_at_cursor:
                        continue
                    f.write(json.dumps(row, cls=_Encoder) + "\n")
                    count += 1
                    if ts_val and ts_val != cursor:
                        cursor = ts_val
                        seen_at_cursor = set()
                    if count % MAX_PAGE_SIZE == 0:
                        f.flush()
                    if insert_id:
                        seen_at_cursor.add(insert_id)
                    if max_entries is not None and count >= max_entries:
                        truncated = True
                        break
                break
            except Exception as exc:  # noqa: BLE001 - re-raised unless it is a quota error
                if not _is_quota_error(exc):
                    raise
                quota_retries += 1
                f.flush()
                if quota_retries > MAX_QUOTA_RETRIES:
                    status = "partial"
                    note = (
                        f"Quota exhausted {quota_retries}x at {limiter.rpm} req/min. "
                        f'Re-run with start_time="{cursor}" to resume; '
                        "entries already written are kept."
                    )
                    break
                # Wait out the server's one-minute quota window, then resume at the cursor.
                time.sleep(60.0 * quota_retries)

    if truncated:
        status = "truncated"
        note = (
            f"Stopped at max_entries={max_entries}; more entries match this filter. "
            f'Re-run with start_time="{cursor}" to continue.'
        )
    if status != "complete":
        note += " Pass resume_skip_insert_ids back as skip_insert_ids to avoid re-writing "
        note += "the entries that share that timestamp."

    result: dict[str, Any] = {
        "file": output_file,
        "entry_count": count,
        "bytes_written": os.path.getsize(output_file),
        "filter": full_filter,
        "status": status,
        "api_requests": limiter.requests,
        "quota_waits_seconds": round(limiter.wait_seconds, 1),
        "quota_retries": quota_retries,
        "elapsed_seconds": round(time.monotonic() - began, 1),
    }
    if status != "complete":
        result["resume_from"] = cursor
        result["resume_skip_insert_ids"] = sorted(seen_at_cursor)
        result["note"] = note
    return result
