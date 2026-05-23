"""Entry point — `python -m scribble_mcp` or `scribble-mcp` runs the MCP server over stdio."""

import asyncio
from scribble_mcp.server import run_stdio


def main():
    asyncio.run(run_stdio())


if __name__ == "__main__":
    main()