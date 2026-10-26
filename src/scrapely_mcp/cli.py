"""Console entry points: `scrapely-mcp-server` (stdio) and `scrapely-mcp-http` (hosted)."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .config import default_ui_option, env_bool, load_settings, parse_ui_option
from .log import log, register_secret, setup_logging

CREDENTIAL_FILES = ("~/.scrapely/credentials.json", "~/.scrapely/auth.json")


def init_sentry() -> None:
    dsn = os.environ.get("SENTRY_DSN")
    if not dsn:
        return
    try:
        import sentry_sdk

        sentry_sdk.init(dsn=dsn, traces_sample_rate=0.0, send_default_pii=False, release=f"scrapely-mcp@{__version__}")
    except Exception as exc:  # noqa: BLE001 - never block startup
        log.warning("Sentry initialisation failed", reason=str(exc))


def _capture(exc: BaseException) -> None:
    try:
        import sentry_sdk

        sentry_sdk.capture_exception(exc)
        sentry_sdk.flush(5)
    except Exception:  # noqa: BLE001
        pass


def find_token() -> str | None:
    token = os.environ.get("SCRAPELY_TOKEN")
    if token and token.strip():
        return token.strip()
    for name in CREDENTIAL_FILES:
        path = Path(os.path.expanduser(name))
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and isinstance(data.get("token"), str) and data["token"].strip():
            return data["token"].strip()
    return None


def _stdio_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="scrapely-mcp-server", description="Scrape.ly MCP server over stdio.")
    p.add_argument("--actors", default=None, help="Comma-separated Actor names to expose as tools.")
    p.add_argument("--tools", default=None, help="Comma-separated tool categories or tool names.")
    p.add_argument("--telemetry-enabled", dest="telemetry", action="store_true", default=None)
    p.add_argument("--no-telemetry-enabled", dest="telemetry", action="store_false")
    p.add_argument("--telemetry-env", default="PROD", choices=["PROD", "DEV", "prod", "dev"])
    p.add_argument("--ui", default=None, help="auto (default), apps or default.")
    p.add_argument("--version", action="version", version=f"scrapely-mcp {__version__}")
    return p


async def _run_stdio(args: argparse.Namespace) -> int:
    from mcp.server.stdio import stdio_server

    from .selection import normalize_selection, token_required
    from .server import ScrapelyServer, build_state

    os.environ.setdefault("SCRAPELY_TELEMETRY_ENV", args.telemetry_env.upper())
    settings = load_settings()
    selection = normalize_selection(args.actors, args.tools)
    token = find_token()
    register_secret(token)
    if token_required(selection) and not token:
        log.error(
            "A Scrape.ly API token is required. Set the SCRAPELY_TOKEN environment variable or log in with the CLI "
            "so that ~/.scrapely/credentials.json contains a token."
        )
        return 1
    telemetry_on = args.telemetry if args.telemetry is not None else env_bool("TELEMETRY_ENABLED", False)
    ui = parse_ui_option(args.ui) if args.ui is not None else default_ui_option()
    state = await build_state(
        settings,
        token=token,
        payment=None,
        ui_option=ui,
        telemetry_enabled=telemetry_on,
        transport="stdio",
        selection=selection,
    )
    state.task_scope = f"stdio:{state.session_id}"
    server = ScrapelyServer(state)
    log.info("starting stdio server", tools=len(state.tools))
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        from . import session_log, telemetry

        if state.session_log is not None:
            state.session_log.end("eof")
        await session_log.flush_all()
        await telemetry.flush()
    return 0


def stdio_main(argv: list[str] | None = None) -> None:
    init_sentry()
    setup_logging()
    args = _stdio_parser().parse_args(argv)
    import anyio

    try:
        code = anyio.run(_run_stdio, args)
    except KeyboardInterrupt:
        code = 0
    except BaseException as exc:  # noqa: BLE001
        if isinstance(exc, SystemExit):
            raise
        if _is_keyboard_interrupt(exc):
            code = 0
        else:
            log.error("stdio server stopped after a fatal error", exc=True)
            _capture(exc)
            code = 1
    sys.exit(code)


def _is_keyboard_interrupt(exc: BaseException) -> bool:
    if isinstance(exc, KeyboardInterrupt):
        return True
    inner = getattr(exc, "exceptions", None)
    return bool(inner) and all(_is_keyboard_interrupt(e) for e in inner)


def http_main(argv: list[str] | None = None) -> None:
    init_sentry()
    setup_logging()
    p = argparse.ArgumentParser(prog="scrapely-mcp-http", description="Scrape.ly MCP server over streamable HTTP.")
    p.add_argument("--host", default=os.environ.get("SCRAPELY_MCP_HOST") or "0.0.0.0")
    p.add_argument("--port", type=int, default=None)
    p.add_argument("--actors", default=os.environ.get("SCRAPELY_ACTORS"))
    p.add_argument("--tools", default=os.environ.get("SCRAPELY_TOOLS"))
    p.add_argument("--ui", default=None)
    p.add_argument("--version", action="version", version=f"scrapely-mcp {__version__}")
    args = p.parse_args(argv)
    port = args.port
    if port is None:
        try:
            port = int(os.environ.get("SCRAPELY_MCP_PORT") or 8000)
        except ValueError:
            port = 8000
    ui = parse_ui_option(args.ui) if args.ui is not None else default_ui_option()

    import uvicorn

    from .http_app import Gateway

    app = Gateway(default_actors=args.actors, default_tools=args.tools, ui_option=ui)
    log.info("starting HTTP server", host=args.host, port=port, version=__version__)
    try:
        uvicorn.run(app, host=args.host, port=port, lifespan="on", log_level="warning", proxy_headers=False)
    except KeyboardInterrupt:
        pass
