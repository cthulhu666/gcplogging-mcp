# gcplogging-mcp

MCP server for bulk downloading Google Cloud Logging entries to local files for offline analysis.

## Tools

| Tool | Description |
|------|-------------|
| `list_projects` | List configured GCP projects |
| `list_logs` | List available log names in a project |
| `download_logs` | Fetch log entries and write to a local JSONL file |

### `download_logs` usage

```
project:     Config key, e.g. "production"
filter:      Cloud Logging filter, e.g. 'resource.type="cloud_run_revision" AND severity>=ERROR'
start_time:  ISO 8601, e.g. "2024-01-20T00:00:00Z"
end_time:    ISO 8601 (optional, defaults to now)
output_file: Absolute path (optional, defaults to /tmp/gcplogging-{project}-{timestamp}.jsonl)
```

Returns `{ "file": "/tmp/...", "entry_count": 42301, "bytes_written": 15234567, "filter": "..." }`.

Output is JSONL — one JSON object per line with `timestamp`, `severity`, `log_name`, `resource`, `payload`, `labels`.

## Setup

### 1. Install dependencies

```sh
cd gcplogging-mcp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 2. Create config

```sh
mkdir -p ~/.config/gcplogging-mcp
cp example-config.toml ~/.config/gcplogging-mcp/config.toml
chmod 600 ~/.config/gcplogging-mcp/config.toml
# edit the file with your project IDs and credentials
```

If `credentials_file` is omitted, Application Default Credentials are used (`gcloud auth application-default login`). The service account needs the `roles/logging.viewer` role.

### 3. Create wrapper script

```sh
#!/bin/sh
export GCPLOGGING_MCP_CONFIG="$HOME/.config/gcplogging-mcp/config.toml"
cd /path/to/gcplogging-mcp
exec .venv/bin/python3 -m gcplogging_mcp.server
```

### 4. Register with your MCP client

Point your MCP client at the wrapper script. For a TOML-based config:

```toml
[mcp_servers.gcplogging]
command = "/path/to/wrapper/gcplogging-mcp"
startup_timeout_sec = 30.0
tool_timeout_sec = 300.0
```

## Filter examples

```
# Cloud Run service logs
resource.type="cloud_run_revision" AND resource.labels.service_name="my-service"

# Errors only
resource.type="cloud_run_revision" AND severity>=ERROR

# Load balancer access logs
resource.type="http_load_balancer"

# Text search
resource.type="cloud_run_revision" AND textPayload:"Exception"
```
