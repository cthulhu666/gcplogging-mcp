# CLAUDE.md

Guidance for Claude Code and Codex when working in this repository.

## Design Principles

### Code Style
- Pure Python 3.12 stdlib plus `google-cloud-logging`; no other runtime dependencies.
- No external MCP SDK — implement the JSON-RPC 2.0 / MCP protocol manually over stdio.
- Minimal abstractions: no class hierarchies, no dependency injection, no unnecessary layers.
- Module structure: `config.py` (TOML loading), `logs.py` (API calls + file writing), `server.py` (MCP stdio loop).

### Safety
- Config files containing credentials must have permissions checked at load time (`& 0o077 == 0`). Fail loudly if world- or group-readable.
- Never write credentials to the output JSONL file.
- `download_logs` is read-only by design — Cloud Logging list API never mutates.

### MCP Protocol
- Support dual framing: Content-Length headers (Claude Code) and newline-delimited JSON (Codex). `_RESPONSE_MODE` flips on first message received.
- Handle: `initialize`, `notifications/initialized`, `ping`, `tools/list`, `tools/call`. Return `-32601` for unknown methods, `-32000` for tool errors.
- Tool results always include both `content` (text) and `structuredContent` (JSON object).

### Configuration
- Connection details live in a TOML file outside the repo (`~/.config/gcplogging-mcp/config.toml` or `GCPLOGGING_MCP_CONFIG` env var).
- Multiple projects supported in one config file.
- Per-project credential override; global `[settings].credentials_file` fallback; ADC if neither set.

## Git Workflow
- Work on a feature branch; never commit directly to `master`.
- Before non-trivial changes, outline the approach for review.
- Bump `VERSION` (SemVer) for any change to tool behavior or protocol.

## Definition of Done
- Implementation is complete for the requested scope.
- Self-review completed: diff vs requested scope, no unintended changes, no secrets in untracked files.
- `VERSION` bumped if tool behavior changed.
- Known verification gaps are explicitly named, not silently skipped.
