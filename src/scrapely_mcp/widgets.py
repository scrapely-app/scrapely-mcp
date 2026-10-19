"""MCP Apps widget resources and `_meta` (SPEC 14).

Widgets are disabled by default (SCRAPELY_MCP_WIDGETS_ENABLED). No HTML assets are
bundled; when widgets are enabled, assets are read from SCRAPELY_MCP_WIDGET_ASSETS_DIR.
A registered widget whose asset is missing is not listed and cannot be read.
"""

from __future__ import annotations

import os

from .config import Settings

WIDGET_MIME = "text/html;profile=mcp-app"

WIDGETS = {
    "search": {"uri": "ui://widget/search-actors.html", "name": "search-actors-widget", "file": "search-actors.html"},
    "run": {"uri": "ui://widget/actor-run.html", "name": "actor-run-widget", "file": "actor-run.html"},
}

_TEXTS = {
    "search": ("Searching the Store…", "Results ready"),
    "run": ("Starting the run…", "Run widget ready"),
}


def widget_meta(settings: Settings, kind: str, description: str | None = None) -> dict:
    connect = [settings.public_api_base_url]
    resources = list(settings.widget_resource_domains)
    invoking, invoked = _TEXTS[kind]
    meta = {
        "ui": {
            "resourceUri": WIDGETS[kind]["uri"],
            "visibility": ["model", "app"],
            "prefersBorder": True,
            "csp": {"connectDomains": connect, "resourceDomains": resources},
        },
        "openai/toolInvocation/invoking": invoking,
        "openai/toolInvocation/invoked": invoked,
        "openai/widgetCSP": {"connect_domains": connect, "resource_domains": resources},
        "openai/widgetDomain": settings.site_url,
    }
    if description:
        meta["openai/widgetDescription"] = description
    return meta


def asset(settings: Settings, kind: str) -> str | None:
    if not settings.widgets_enabled or not settings.widget_assets_dir:
        return None
    path = os.path.join(settings.widget_assets_dir, WIDGETS[kind]["file"])
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return None


def by_uri(uri: str) -> str | None:
    for kind, w in WIDGETS.items():
        if w["uri"] == uri:
            return kind
    return None
