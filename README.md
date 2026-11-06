# Scribble MCP Vault API

The repository name is historical. The implementation is a small HTTP API
used by Claude's n8n MCP vault workflows.

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

### iCloud cloud-only files

macOS File Provider can evict a file's local contents while leaving its path
visible. An ordinary read may then fail with `Resource deadlock avoided`
(`EDEADLK`); that does not imply an n8n or application-thread deadlock.

The Mac API now tries a normal read first, then uses a native
`NSFileCoordinator` read for this specific error. This materializes the
canonical iCloud file without modifying its contents or using a stale mirror.
It covers read/search requests and the existing-content reads required before
write/append. Other errors, including permission failures, are not treated as
hydration failures.

The helper is compiled by `deploy/install-macos.command` before the service is
stopped. It requires Apple's Command Line Tools. To build it separately:

```bash
xcrun swiftc -O vault_coordinated_read.swift -o vault_coordinated_read
```

The compiled executable is local build output and is not tracked in Git.
Hydration is bounded to 15 seconds; if it times out or fails, the API returns
HTTP 503 with `Retry-After: 3`. A failed existing-content read prevents any
write/append commit. Read requests may be retried; do not blindly replay
append/write after an unknown outcome—read the exact target first.

Finder's **Keep Downloaded** option on the vault folder is an additional
eviction-prevention measure. It is not enabled by this release and does not
replace the API's coordinated-read handling.

Run the regression suite from the repository root:

```bash
python3 -m unittest discover -s tests -v
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
- Normal Markdown note mutations go through `vault_mutations.py`.
- Appending or overwriting an existing frontmatter note bumps `updated:` to the API host's local date and preserves `created:` on overwrite.
- New Markdown notes must contain valid schema frontmatter; missing or malformed metadata is rejected before writing.
- `wiki/log.md`, `wiki/index.md`, `wiki/SCHEMA.md`, `wiki/raw/**`, non-Markdown files, and Markdown without frontmatter are metadata-exempt.
- macOS mutations use a same-directory temporary file, `fsync`, and atomic replacement; a failed commit leaves the previous file intact.
- Linux mutations use the same metadata and per-file locking layer, but commit in place because rename-based replacement is not reliable on the iCloud FUSE mount.
- Successful write/append responses are JSON with `ok`, `path`, `operation`, and `metadata_updated` fields; enforced note mutations also include `updated`.

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

## Client

`vault_api_client.py` is the auditable HTTP client for the API (`health`, `read`,
`list`, `search`, `write`, `append`, `delete`, `move`, `batch`). Every mutation is
read back before it reports success, and `delete` and `move` both send the digest of the content
they just read, so a note edited in between is refused rather than clobbered.
