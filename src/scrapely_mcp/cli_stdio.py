"""`python -m scrapely_mcp.cli_stdio` runs the stdio server (same as `scrapely-mcp-server`)."""

from .cli import stdio_main

if __name__ == "__main__":
    stdio_main()
