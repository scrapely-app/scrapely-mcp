"""Configuration read from environment variables.

Every value has a default and can be overridden through the environment. Settings
are read when a server or gateway is built, so tests can change the environment
between builds.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from urllib.parse import urlsplit

DEFAULT_PUBLIC_API_ORIGIN = "https://api.scrape.ly"
DEFAULT_SITE_ORIGIN = "https://scrape.ly"
DEFAULT_STORE_URL = "https://scrape.ly/store"
# The public Scrape.ly docs: search reads <docs>/search-index.json, fetch reads <docs>/<page>.md.
DEFAULT_DOCS_URL = "https://scrape.ly/docs"
DEFAULT_REPORT_PROBLEM_BLOCKED = "anthropic,claude"
DEFAULT_OFFICIAL_USERNAMES = "scrapely"

INTERNAL_HOSTS = {"server", "localhost", "127.0.0.1", "0.0.0.0", "host.docker.internal", "mcp"}

# Global switch: when False, the "auto" UI option always resolves to "default".
WIDGETS_DEFAULT_ENABLED = False

# Legacy request-meta credential alias (`_meta.apifyToken`). Off by default (SPEC 5.8).
ACCEPT_LEGACY_TOKEN_META_ALIAS = False

INLINE_LIMIT_BYTES = 262_144
DATASET_SIZE_HINT_BYTES = 50_000
MAX_WAIT_SECS = 45
DEFAULT_WAIT_SECS = 30

# Memory bounds for data an Actor (or its MCP server) controls.
# One tool result (task or not); the task store uses the same limit.
DEFAULT_MAX_TOOL_RESULT_BYTES = 4 * 1024 * 1024
# Tool definitions one Actor may add to a session, and all Actors together.
DEFAULT_MAX_ACTOR_TOOL_BYTES = 1024 * 1024
DEFAULT_MAX_SESSION_TOOL_BYTES = 4 * 1024 * 1024
# Actor tool definitions all sessions of the process may keep together. Python
# holds about 3x these JSON bytes, so 64 MiB stays well inside the 1 GiB container.
DEFAULT_MAX_TOTAL_TOOL_BYTES = 64 * 1024 * 1024
# What one tools/list connection to an MCP-server Actor may send back.
DEFAULT_MAX_REMOTE_TOOL_LIST_BYTES = 4 * 1024 * 1024


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        return default
    return value


def _env_int(name: str, default: int, minimum: int | None = None) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    if minimum is not None and value < minimum:
        return default
    return value


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        return default
    if value <= 0 or value != value or value == float("inf"):
        return default
    return value


def parse_bool(raw: str | None) -> bool | None:
    if raw is None:
        return None
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return None


def env_bool(name: str, default: bool = False) -> bool:
    parsed = parse_bool(os.environ.get(name))
    return default if parsed is None else parsed


def is_internal_host(host: str | None) -> bool:
    if not host:
        return True
    h = host.strip().lower().strip("[]")
    return h in INTERNAL_HOSTS or h.endswith(".internal")


def is_public_https(url: str | None) -> bool:
    if not url:
        return False
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    return parts.scheme == "https" and not is_internal_host(parts.hostname)


def strip_slash(url: str) -> str:
    return url.strip().rstrip("/")


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


@dataclass
class DocSourceConfig:
    source_id: str
    app_id: str
    api_key: str
    index: str
    filters: str | None = None
    type_filter: str | None = None
    facet_filters: list | None = None

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.api_key and self.index)


@dataclass
class Settings:
    api_base_url: str = DEFAULT_PUBLIC_API_ORIGIN
    public_api_base_url: str = DEFAULT_PUBLIC_API_ORIGIN
    issuer: str = DEFAULT_PUBLIC_API_ORIGIN
    store_url: str = DEFAULT_SITE_ORIGIN
    site_url: str = DEFAULT_SITE_ORIGIN
    console_base_url: str = DEFAULT_SITE_ORIGIN
    console_token_prefix: str = ""
    standby_base_url: str = DEFAULT_PUBLIC_API_ORIGIN
    report_problem_blocked: list[str] = field(default_factory=list)
    official_usernames: set[str] = field(default_factory=set)
    session_idle_timeout: float = 1800.0
    max_sessions: int = 10_000
    # Per-key session caps: one presented credential (token fingerprint) and
    # one client IP (as forwarded by the API server's /mcp proxy).
    max_sessions_per_credential: int = 50
    max_sessions_per_ip: int = 100
    # Shared cap for sessions opened without a token (payment-only callers),
    # so they can never hold more than this share of max_sessions.
    max_anonymous_sessions: int = 1000
    # Per-network cap inside that shared pool (IPv4 address or IPv6 /48), so a
    # few addresses cannot fill it for every other payment-only caller.
    max_anonymous_sessions_per_ip: int = 10
    # Payment-only sessions cost nothing to open, so idle ones are reclaimed sooner.
    anonymous_session_idle_timeout: float = 300.0
    # Most Actors one ?actors= / ?tools= selection may name.
    max_selected_actors: int = 50
    # Byte bounds for Actor-provided tool definitions and for one tool result.
    max_actor_tool_bytes: int = DEFAULT_MAX_ACTOR_TOOL_BYTES
    max_session_tool_bytes: int = DEFAULT_MAX_SESSION_TOOL_BYTES
    max_total_tool_bytes: int = DEFAULT_MAX_TOTAL_TOOL_BYTES
    max_remote_tool_list_bytes: int = DEFAULT_MAX_REMOTE_TOOL_LIST_BYTES
    max_tool_result_bytes: int = DEFAULT_MAX_TOOL_RESULT_BYTES
    telemetry_enabled: bool = False
    telemetry_write_key: str | None = None
    skyfire_seller_service_id: str | None = None
    widgets_enabled: bool = WIDGETS_DEFAULT_ENABLED
    widget_assets_dir: str | None = None
    widget_resource_domains: list[str] = field(default_factory=list)
    send_legacy_payment_header: bool = True
    # Shared with the API server (MCP_CLIENT_IP_SECRET). Signs the caller's IP
    # on x402 calls so the server's x402 attempt limit counts each caller.
    client_ip_secret: str | None = None
    # Session log reported to the Scrape.ly API (session_log.py). SCRAPELY_MCP_SESSION_LOG=false turns it off.
    session_log_enabled: bool = True
    docs_url: str = DEFAULT_DOCS_URL
    docs_sources: dict[str, DocSourceConfig] = field(default_factory=dict)

    @property
    def api_origins(self) -> set[str]:
        """Origins (scheme://host:port) accepted by the resource proxy gate."""
        return {_norm_origin(self.api_base_url), _norm_origin(self.public_api_base_url)}


def _norm_origin(url: str) -> str:
    parts = urlsplit(url)
    port = parts.port
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    return f"{parts.scheme}://{(parts.hostname or '').lower()}:{port}"


def normalized_origin(url: str) -> str:
    return _norm_origin(url)


def compute_issuer() -> str:
    for name in ("SCRAPELY_PUBLIC_API_BASE_URL", "API_BASE_URL", "SCRAPELY_API_BASE_URL"):
        value = os.environ.get(name)
        if value and is_public_https(value):
            return strip_slash(value)
    return DEFAULT_PUBLIC_API_ORIGIN


def load_settings() -> Settings:
    api_base = strip_slash(_env("SCRAPELY_API_BASE_URL") or DEFAULT_PUBLIC_API_ORIGIN)
    public_env = _env("SCRAPELY_PUBLIC_API_BASE_URL")
    if public_env and public_env.strip():
        public_base = strip_slash(public_env)
    elif is_public_https(api_base):
        public_base = api_base
    else:
        public_base = DEFAULT_PUBLIC_API_ORIGIN
    site = DEFAULT_SITE_ORIGIN
    store = strip_slash(_env("SCRAPELY_STORE_URL") or DEFAULT_STORE_URL)
    blocked_raw = _env("SCRAPELY_MCP_REPORT_PROBLEM_BLOCKED_CLIENTS", DEFAULT_REPORT_PROBLEM_BLOCKED) or ""
    official_raw = _env("SCRAPELY_OFFICIAL_USERNAMES", DEFAULT_OFFICIAL_USERNAMES) or ""
    resource_domains_raw = _env("SCRAPELY_MCP_WIDGET_RESOURCE_DOMAINS")
    if resource_domains_raw:
        resource_domains = [d.strip() for d in resource_domains_raw.split(",") if d.strip()]
    else:
        resource_domains = [site, public_base, "https://fonts.googleapis.com", "https://fonts.gstatic.com"]

    write_key = _env("SCRAPELY_TELEMETRY_WRITE_KEY")
    if (_env("SCRAPELY_TELEMETRY_ENV") or "PROD").upper() == "DEV":
        write_key = _env("SCRAPELY_TELEMETRY_WRITE_KEY_DEV") or None

    return Settings(
        api_base_url=api_base,
        public_api_base_url=public_base,
        issuer=compute_issuer(),
        store_url=store,
        site_url=strip_slash(_env("SCRAPELY_SITE_URL") or site),
        console_base_url=strip_slash(_env("SCRAPELY_CONSOLE_BASE_URL") or site),
        console_token_prefix=(_env("SCRAPELY_CONSOLE_TOKEN_PREFIX") or "").strip(),
        standby_base_url=strip_slash(_env("SCRAPELY_STANDBY_BASE_URL") or api_base),
        report_problem_blocked=[s.strip().lower() for s in blocked_raw.split(",") if s.strip()],
        official_usernames={s.strip().lower() for s in official_raw.split(",") if s.strip()},
        session_idle_timeout=_env_float("SCRAPELY_MCP_SESSION_IDLE_TIMEOUT_SECS", 1800.0),
        max_sessions=_env_int("SCRAPELY_MCP_MAX_SESSIONS", 10_000, minimum=1),
        max_sessions_per_credential=_env_int("SCRAPELY_MCP_MAX_SESSIONS_PER_CREDENTIAL", 50, minimum=1),
        max_sessions_per_ip=_env_int("SCRAPELY_MCP_MAX_SESSIONS_PER_IP", 100, minimum=1),
        max_anonymous_sessions=_env_int("SCRAPELY_MCP_MAX_ANONYMOUS_SESSIONS", 1000, minimum=1),
        max_anonymous_sessions_per_ip=_env_int("SCRAPELY_MCP_MAX_ANON_SESSIONS_PER_IP", 10, minimum=1),
        anonymous_session_idle_timeout=_env_float("SCRAPELY_MCP_ANON_SESSION_IDLE_TIMEOUT_SECS", 300.0),
        max_selected_actors=_env_int("SCRAPELY_MCP_MAX_SELECTED_ACTORS", 50, minimum=1),
        max_actor_tool_bytes=_env_int("SCRAPELY_MCP_MAX_ACTOR_TOOL_BYTES", DEFAULT_MAX_ACTOR_TOOL_BYTES, minimum=1),
        max_session_tool_bytes=_env_int(
            "SCRAPELY_MCP_MAX_SESSION_TOOL_BYTES", DEFAULT_MAX_SESSION_TOOL_BYTES, minimum=1
        ),
        max_total_tool_bytes=_env_int("SCRAPELY_MCP_MAX_TOTAL_TOOL_BYTES", DEFAULT_MAX_TOTAL_TOOL_BYTES, minimum=1),
        max_remote_tool_list_bytes=_env_int(
            "SCRAPELY_MCP_MAX_REMOTE_TOOL_LIST_BYTES", DEFAULT_MAX_REMOTE_TOOL_LIST_BYTES, minimum=1
        ),
        max_tool_result_bytes=_env_int("SCRAPELY_MCP_MAX_TOOL_RESULT_BYTES", DEFAULT_MAX_TOOL_RESULT_BYTES, minimum=1),
        telemetry_enabled=env_bool("TELEMETRY_ENABLED", False),
        telemetry_write_key=write_key or None,
        skyfire_seller_service_id=_env("SKYFIRE_SELLER_SERVICE_ID") or None,
        widgets_enabled=env_bool("SCRAPELY_MCP_WIDGETS_ENABLED", WIDGETS_DEFAULT_ENABLED),
        widget_assets_dir=_env("SCRAPELY_MCP_WIDGET_ASSETS_DIR") or None,
        widget_resource_domains=resource_domains,
        send_legacy_payment_header=env_bool("SCRAPELY_SEND_LEGACY_PAYMENT_HEADER", True),
        client_ip_secret=(_env("SCRAPELY_MCP_CLIENT_IP_SECRET") or "").strip() or None,
        session_log_enabled=env_bool("SCRAPELY_MCP_SESSION_LOG", True),
        docs_url=strip_slash(_env("SCRAPELY_DOCS_URL") or DEFAULT_DOCS_URL),
        docs_sources=_load_doc_sources(),
    )


def _load_doc_sources() -> dict[str, DocSourceConfig]:
    def env3(prefix: str, app: str, key: str, index: str) -> tuple[str, str, str]:
        return (
            (_env(f"{prefix}_APP_ID", app) or "").strip(),
            (_env(f"{prefix}_API_KEY", key) or "").strip(),
            (_env(f"{prefix}_INDEX", index) or "").strip(),
        )

    js_app, js_key, js_index = env3(
        "SCRAPELY_DOCS_CRAWLEE_JS", "5JC94MPMLY", "267679200b833c2ca1255ab276731869", "crawlee"
    )
    py_app, py_key, py_index = env3(
        "SCRAPELY_DOCS_CRAWLEE_PY", "5JC94MPMLY", "878493fcd7001e3c179b6db6796a999b", "crawlee_python"
    )
    return {
        "crawlee-js": DocSourceConfig(
            "crawlee-js",
            js_app,
            js_key,
            js_index,
            type_filter="lvl1",
            facet_filters=["language:en", ["docusaurus_tag:default", "docusaurus_tag:docs-default-3.15"]],
        ),
        "crawlee-py": DocSourceConfig(
            "crawlee-py",
            py_app,
            py_key,
            py_index,
            type_filter="lvl1",
            facet_filters=["language:en", ["docusaurus_tag:docs-default-current"]],
        ),
    }


def parse_ui_option(raw: str | None) -> str:
    if raw is None:
        return "auto"
    value = raw.strip().lower()
    if value in {"apps", "true", "on", "openai"}:
        return "apps"
    if value in {"default", "false", "off"}:
        return "default"
    return "auto"


def default_ui_option() -> str:
    raw = os.environ.get("SCRAPELY_MCP_UI")
    if raw is None:
        raw = os.environ.get("UI_MODE")
    return parse_ui_option(raw)
