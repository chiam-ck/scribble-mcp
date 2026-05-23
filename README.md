# Scribble MCP 📝

A lightweight MCP server for reading, searching, and writing to a markdown LLM wiki vault. Designed for LLM agents that need persistent knowledge storage.

https://github.com/samurai-bot/scribble-mcp

## Features

| Tool | Description |
|------|-------------|
| `vault_list` | List notes, optionally filtered by type (concept, entity, comparison, query, raw) |
| `vault_read` | Read a note by exact path or glob pattern |
| `vault_search` | Full-text search across all markdown files with snippets |
| `vault_create` | Create a new note with proper YAML frontmatter and date-prefixed filename |
| `vault_append` | Append content to an existing note, auto-bumping the `updated` date |

## Quick Start

```bash
# Install
git clone https://github.com/samurai-bot/scribble-mcp.git
cd scribble-mcp
uv venv && source .venv/bin/activate && uv pip install -e .

# Run as stdio MCP server
python -m scribble_mcp

# Or run the HTTP server (for webhook integration)
python vault_api.py
```

### Claude Desktop / Claude Code

Add to your MCP config:

```json
{
  "mcpServers": {
    "scribble-wiki": {
      "command": "python3",
      "args": ["-m", "scribble_mcp"],
      "cwd": "/path/to/scribble-mcp"
    }
  }
}
```

## Configuration

Copy `.env.example` to `.env` and customize:

| Variable | Default | Description |
|----------|---------|-------------|
| `VAULT_API_PORT` | `8999` | HTTP server port (only used for webhook mode) |
| `VAULT_API_HOST` | `0.0.0.0` | HTTP server bind address |
| `VAULT_PATH` | `~/vault/wiki` | Path to your markdown wiki vault |

## Vault Structure

The vault expects a directory of markdown files organized by type:

```
wiki/
├── concepts/       # Knowledge topics
├── entities/       # Named things (people, projects, places)
├── comparisons/    # Side-by-side analyses
├── queries/        # Saved Q&A pairs
└── raw/            # Source materials
```

Files follow the naming convention `YYYY-MM-DD-descriptive-slug.md` with YAML frontmatter:

```yaml
---
title: Page Title
created: 2025-01-01
updated: 2025-01-01
type: concept
tags: [tag1, tag2]
sources: []
---
```

## HTTP API (Webhook Mode)

For integration with n8n or other platforms that can't run stdio MCP:

```
GET  /health         — Health check
GET  /list?type=X    — List notes by type
GET  /read?path=X    — Read a specific note
GET  /search?q=X     — Full-text search
POST /create         — Create a note (JSON body)
POST /append         — Append to a note (JSON body)
POST /               — Legacy format (operation/directory/query/path/content)
```

## License

MIT
