# Scribble MCP Vault API

The repository name is historical. The implementation is a small,
dependency-free HTTP API used by Claude's n8n MCP vault workflows.

The repository keeps separate deployment entry points for the original Ubuntu
host and the Mac mini. The Ubuntu implementation is intentionally preserved.

## Production architecture

```text
Claude MCP
  → n8n over private Tailscale
  → vault-api on the selected host
  → Obsidian vault
```

## Ubuntu/Linux deployment (preserved)

The original Linux implementation remains in:

```text
vault_api.py
deploy/vault-api.service
```

It expects the FUSE-mounted vault at:

```text
/home/ck/icloud-linux-mount/Obsidian/Ck's Vault
```

The service checks the mount and `icloud.service` before vault operations.
Run it with the existing systemd unit:

```bash
systemctl --user daemon-reload
systemctl --user enable --now vault-api.service
systemctl --user status vault-api.service
python3 vault_api.py 100.78.128.119 8765
```

The former Ubuntu endpoint was `100.78.128.119:8765`. That host is retired,
but its code and deployment definition are retained here for portability and
history.

## macOS deployment

The Mac-specific implementation is separate:

```text
vault_api_macos.py
deploy/com.ck.scribble-mcp.vault-api.plist
deploy/install-macos.command
```

The live Mac mini checkout and LaunchAgent are:

```text
/Users/ck/homelab/scribble-mcp
/Users/ck/Library/LaunchAgents/com.ck.scribble-mcp.vault-api.plist
```

The service uses the native iCloud File Provider vault:

```text
/Users/ck/Library/Mobile Documents/iCloud~md~obsidian/Documents/Ck's Vault
```

It binds to the Mac mini's Tailscale address:

```text
100.90.117.53:8765
```

`launchd` starts it at login and keeps it alive after a process failure.
Install or reload it outside the Hermes gateway with:

```bash
./deploy/install-macos.command
```

Inspect it with:

```bash
launchctl print gui/$(id -u)/com.ck.scribble-mcp.vault-api
curl http://100.90.117.53:8765/health
```

## API contract

All paths are relative to the vault root. Path traversal and absolute paths are rejected.

| Method | Endpoint | Request | Purpose |
|---|---|---|---|
| GET | `/health` | none | Check vault health |
| GET | `/vault/read?path=wiki/SCHEMA.md` | none | Read a file |
| GET | `/vault/list?path=wiki/` | none | List a directory |
| GET | `/vault/search?q=frontmatter&path=wiki/` | none | Case-sensitive regex search |
| POST | `/vault/write` | `{path, content}` | Write or overwrite a file |
| POST | `/vault/append` | `{path, content}` | Append to a file |
| POST | `/vault/delete` | `{path}` | Delete a file |
| POST | `/vault/move` | `{from, to}` | Move or rename a file |

The n8n MCP workflow parameter names are part of the contract:

- Search uses `q`
- Move uses `from` and `to`
- Write and append use `path` and `content`
- Delete uses `path`

## Write safety

- The vault root and `wiki/` directory are checked before operations.
- Relative paths are required and path traversal is rejected.
- Writes use direct file operations.
- Existing files are not replaced through a generic temp-file-plus-rename workflow.

## n8n verification

The seven live Vault workflows are:

- `Vault Read - MCP`
- `Vault Write - MCP`
- `Vault Append - MCP`
- `Vault Delete - MCP`
- `Vault Move - MCP`
- `Vault List - MCP`
- `Vault Search - MCP`

For an end-to-end check, use a temporary file under `wiki/`, exercise write,
read, append, move, and delete, then confirm the file is gone. Also exercise
list and search. Never leave test files in the vault.
