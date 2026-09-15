"""Minimal stdio MCP server for bulk Google Cloud Logging downloads."""

from __future__ import annotations

import io
import json
import signal
import sys
import contextlib
from typing import Any

from gcplogging_mcp import config, logs


PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "gcplogging-mcp"
SERVER_VERSION = "1.1.0"
_RESPONSE_MODE = "content-length"
_CLIENT_NAME: str = ""


def _tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "list_projects",
            "description": "List all configured GCP projects.",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": "list_logs",
            "description": "List available log names in a configured GCP project.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project name as defined in config (e.g. 'production').",
                    },
                },
                "required": ["project"],
                "additionalProperties": False,
            },
        },
        {
            "name": "download_logs",
            "description": (
                "Download log entries from a GCP project matching a filter and time range, "
                "saving them to a local JSONL file. Returns the file path and entry count.\n\n"
                "Page fetches are paced under the project's Cloud Logging read quota "
                "(60 req/min by default), so a large range takes minutes rather than "
                "failing with 429 — narrow the filter to keep it short. On quota "
                "exhaustion or max_entries the call still returns, with status "
                "'partial'/'truncated' plus resume_from; re-run with start_time set to "
                "resume_from to continue into a new file."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Project name as defined in config (e.g. 'production').",
                    },
                    "filter": {
                        "type": "string",
                        "description": (
                            "Cloud Logging filter string, e.g. "
                            "'resource.type=\"gae_app\" AND severity>=ERROR'. "
                            "timestamp constraints are added automatically."
                        ),
                    },
                    "start_time": {
                        "type": "string",
                        "description": "Start of time range in ISO 8601 format, e.g. '2024-01-01T00:00:00Z'.",
                    },
                    "end_time": {
                        "type": "string",
                        "description": "End of time range in ISO 8601 format. Defaults to now.",
                    },
                    "output_file": {
                        "type": "string",
                        "description": (
                            "Absolute path for the output JSONL file. "
                            "Defaults to /tmp/gcplogging-{project}-{timestamp}.jsonl."
                        ),
                    },
                    "max_entries": {
                        "type": "integer",
                        "minimum": 1,
                        "description": (
                            "Stop after this many entries. Returns status 'truncated' and "
                            "resume_from so nothing is silently dropped. Use it to sample a "
                            "noisy filter before committing to the full range."
                        ),
                    },
                    "skip_insert_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "resume_skip_insert_ids from the previous partial/truncated "
                            "result. The resume window includes its start timestamp, so "
                            "without this the entries at that instant are written twice."
                        ),
                    },
                },
                "required": ["project", "filter", "start_time"],
                "additionalProperties": False,
            },
        },
    ]


def _text_result(payload: Any) -> dict[str, Any]:
    if isinstance(payload, list) and "claude" in _CLIENT_NAME.lower():
        structured: Any = {"items": payload}
    else:
        structured = payload
    return {
        "content": [{"type": "text", "text": json.dumps(payload, indent=2, sort_keys=True)}],
        "structuredContent": structured,
    }


def _call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    projects = config.load_projects()

    if name == "list_projects":
        return _text_result([
            {"name": p.name, "project_id": p.project_id, "description": p.description}
            for p in projects.values()
        ])

    project_key = arguments.get("project")
    if not project_key:
        raise ValueError("'project' argument is required")
    if project_key not in projects:
        raise ValueError(f"Unknown project '{project_key}'. Known: {list(projects)}")

    cfg = projects[project_key]

    if name == "list_logs":
        return _text_result(logs.list_logs(cfg))

    if name == "download_logs":
        result = logs.download_logs(
            cfg=cfg,
            filter_str=arguments.get("filter", ""),
            start_time=arguments["start_time"],
            end_time=arguments.get("end_time"),
            output_file=arguments.get("output_file"),
            max_entries=arguments.get("max_entries"),
            skip_insert_ids=arguments.get("skip_insert_ids"),
        )
        return _text_result(result)

    raise ValueError(f"Unknown tool: {name}")


def _run_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    stderr = io.StringIO()
    with contextlib.redirect_stderr(stderr):
        try:
            return _call_tool(name, arguments)
        except SystemExit as exc:
            detail = stderr.getvalue().strip()
            raise RuntimeError(detail or str(exc)) from exc


def _read_message() -> dict[str, Any] | None:
    global _RESPONSE_MODE
    content_length: int | None = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break

        stripped = line.decode("utf-8").strip()
        if stripped.startswith("{"):
            _RESPONSE_MODE = "json-line"
            return json.loads(stripped)

        if not stripped:
            break
        name, _, value = stripped.partition(":")
        if name.lower() == "content-length":
            content_length = int(value.strip())

    if content_length is None:
        raise RuntimeError("Missing Content-Length header")

    body = sys.stdin.buffer.read(content_length)
    if not body:
        return None
    _RESPONSE_MODE = "content-length"
    return json.loads(body.decode("utf-8"))


def _send_message(payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload).encode("utf-8")
    if _RESPONSE_MODE == "json-line":
        sys.stdout.buffer.write(encoded + b"\n")
    else:
        sys.stdout.buffer.write(f"Content-Length: {len(encoded)}\r\n\r\n".encode("utf-8"))
        sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


def _response(message_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def _error(message_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def main() -> None:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    while True:
        message = _read_message()
        if message is None:
            return

        method = message.get("method")
        message_id = message.get("id")
        params = message.get("params") or {}

        try:
            if method == "initialize":
                global _CLIENT_NAME
                _CLIENT_NAME = (params.get("clientInfo") or {}).get("name", "")
                result = {
                    "protocolVersion": PROTOCOL_VERSION,
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                    "capabilities": {"tools": {}},
                }
                if message_id is not None:
                    _send_message(_response(message_id, result))
                continue

            if method == "notifications/initialized":
                continue

            if method == "ping":
                if message_id is not None:
                    _send_message(_response(message_id, {}))
                continue

            if method == "tools/list":
                if message_id is not None:
                    _send_message(_response(message_id, {"tools": _tool_definitions()}))
                continue

            if method == "tools/call":
                result = _run_tool(params.get("name", ""), params.get("arguments") or {})
                if message_id is not None:
                    _send_message(_response(message_id, result))
                continue

            if message_id is not None:
                _send_message(_error(message_id, -32601, f"Method not found: {method}"))

        except Exception as exc:
            if message_id is not None:
                _send_message(_error(message_id, -32000, str(exc)))


if __name__ == "__main__":
    main()
