"""Documentation search and fetch (SPEC 7.23, 7.24)."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

import httpx

from ..api import USER_AGENT
from ..core import CallContext, ok, text
from ..errors import ToolError
from ..util import TTLCache

ALLOWED_DOC_HOSTS = ("docs.scrape.ly", "crawlee.dev")
SENTINEL_PRE = ""
SENTINEL_POST = ""
HITS_PER_QUERY = 50
FETCH_TIMEOUT = 30.0

search_cache: TTLCache[str, list[dict]] = TTLCache(500, 3600)
page_cache: TTLCache[str, str] = TTLCache(500, 3600)


def clear_caches() -> None:
    search_cache.clear()
    page_cache.clear()


async def algolia_query(app_id: str, api_key: str, index: str, body: dict) -> dict:
    url = f"https://{app_id}-dsn.algolia.net/1/indexes/{index}/query"
    headers = {
        "X-Algolia-Application-Id": app_id,
        "X-Algolia-API-Key": api_key,
        "User-Agent": USER_AGENT,
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(url, json=body, headers=headers)
        resp.raise_for_status()
        return resp.json()


async def search_docs(cc: CallContext, args: dict) -> dict:
    source_id = args.get("docSource") or "scrapely"
    query = str(args.get("query") or "").strip()
    limit = int(args.get("limit") or 5)
    offset = int(args.get("offset") or 0)
    if not query:
        raise ToolError("The search query is empty.")
    source = cc.settings.docs_sources.get(source_id)
    if source is None:
        raise ToolError(f"Unknown documentation source '{source_id}'. Use scrapely, crawlee-js or crawlee-py.")
    if not source.configured:
        raise ToolError(
            "The Scrape.ly documentation search is not configured on this server (set SCRAPELY_DOCS_ALGOLIA_APP_ID, "
            "SCRAPELY_DOCS_ALGOLIA_API_KEY and SCRAPELY_DOCS_ALGOLIA_INDEX). The crawlee-js and crawlee-py sources "
            "are available."
        )
    key = f"{source_id}::{query.lower()}"
    results = search_cache.get(key)
    if results is None:
        filters = [f for f in (source.filters, f"type:{source.type_filter}" if source.type_filter else None) if f]
        body: dict = {
            "query": query,
            "hitsPerPage": HITS_PER_QUERY,
            "attributesToSnippet": ["content:100"],
            "snippetEllipsisText": "…",
            "highlightPreTag": SENTINEL_PRE,
            "highlightPostTag": SENTINEL_POST,
            "attributesToHighlight": [],
            "attributesToRetrieve": ["url_without_anchor", "anchor"],
        }
        if filters:
            body["filters"] = " AND ".join(filters)
        if source.facet_filters:
            body["facetFilters"] = source.facet_filters
        try:
            data = await algolia_query(source.app_id, source.api_key, source.index, body)
        except httpx.HTTPError as exc:
            raise ToolError(f"The documentation search failed: {exc}", user=False) from exc
        results = []
        for hit in data.get("hits") or []:
            url = hit.get("url_without_anchor")
            if not url:
                continue
            anchor = hit.get("anchor")
            if isinstance(anchor, str) and anchor.strip():
                url = f"{url}#{anchor.strip()}"
            entry = {"url": url}
            snippet = ((hit.get("_snippetResult") or {}).get("content") or {}).get("value")
            if isinstance(snippet, str):
                snippet = snippet.replace(SENTINEL_PRE, "").replace(SENTINEL_POST, "").strip()
                if snippet:
                    entry["content"] = snippet
            results.append(entry)
        search_cache.set(key, results)
    page = results[offset : offset + limit]
    if not page:
        instructions = "No documentation matched. Try other or fewer keywords, or another docSource."
        return ok({"results": [], "query": query, "count": 0, "instructions": instructions}, [instructions])
    instructions = (
        ("Read a full page with fetch-scrapely-docs. " if cc.has("fetch-scrapely-docs") else "")
        + "When platform docs and tutorials cover the same topic, prefer the platform docs."
    )
    lines = [f"{i + 1}. {r['url']}" + (f"\n   {r['content']}" if r.get("content") else "") for i, r in enumerate(page)]
    result = ok({"results": page, "query": query, "count": len(page), "instructions": instructions}, [])
    result["content"] = [text("\n".join(lines)), text(instructions)]
    return result


def doc_url_allowed(url: str) -> bool:
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme != "https" or parts.username or parts.password or "@" in parts.netloc:
        return False
    return (parts.hostname or "").lower() in ALLOWED_DOC_HOSTS


def markdown_url(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    path = (path + ".md") if path else "/index.md"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


async def fetch_docs(cc: CallContext, args: dict) -> dict:
    url = str(args.get("url") or "").strip()
    if not doc_url_allowed(url):
        raise ToolError(
            "Only documentation pages under https://docs.scrape.ly and https://crawlee.dev can be fetched."
            + (" Find pages with search-scrapely-docs." if cc.has("search-scrapely-docs") else "")
        )
    parts = urlsplit(url)
    clean = urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))
    content = page_cache.get(clean)
    if content is None:
        md = markdown_url(clean)
        try:
            async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True) as client:
                resp = await client.get(md, headers={"User-Agent": USER_AGENT})
        except httpx.HTTPError as exc:
            raise ToolError(f"Fetching {url} failed: {exc}", user=False) from exc
        if resp.status_code >= 400:
            raise ToolError(f"Fetching {url} failed with HTTP status {resp.status_code}.", user=False, http_status=resp.status_code)
        content = resp.text
        page_cache.set(clean, content)
    return ok({"url": url, "content": content}, [f"Documentation page {url}:\n\n{content}"])
