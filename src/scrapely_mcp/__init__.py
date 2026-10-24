"""Scrape.ly MCP server."""

from importlib import metadata as _metadata

try:
    __version__ = _metadata.version("scrapely-mcp")
except _metadata.PackageNotFoundError:  # running from a source tree without install
    __version__ = "0.3.4"

SERVER_NAME = "scrapely-mcp-server"
SERVER_TITLE = "Scrape.ly"

__all__ = ["SERVER_NAME", "SERVER_TITLE", "__version__"]
