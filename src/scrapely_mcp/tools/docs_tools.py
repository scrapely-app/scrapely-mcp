"""Documentation search and fetch (SPEC 7.23, 7.24)."""

from __future__ import annotations

import re
from urllib.parse import urlsplit, urlunsplit

import httpx

from ..api import USER_AGENT
from ..core import CallContext, ok, text
from ..errors import ToolError
from ..util import TTLCache

CRAWLEE_DOC_HOST = "crawlee.dev"
SENTINEL_PRE = ""
SENTINEL_POST = ""
HITS_PER_QUERY = 50
FETCH_TIMEOUT = 30.0
# The Scrape.ly docs index (<docs>/search-index.json) is about 1 MB; refuse anything far larger.
MAX_INDEX_BYTES = 16 * 1024 * 1024
SNIPPET_CHARS = 240

search_cache: TTLCache[str, list[dict]] = TTLCache(500, 3600)
page_cache: TTLCache[str, str] = TTLCache(500, 3600)
index_cache: TTLCache[str, list[dict]] = TTLCache(4, 3600)


def clear_caches() -> None:
    search_cache.clear()
    page_cache.clear()
    index_cache.clear()


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


async def scrapely_index(cc: CallContext) -> list[dict]:
    """The Scrape.ly docs search index: one entry per page, split into its h2/h3 sections."""
    url = f"{cc.settings.docs_url}/search-index.json"
    entries = index_cache.get(url)
    if entries is not None:
        return entries
    try:
        async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            resp.raise_for_status()
            if len(resp.content) > MAX_INDEX_BYTES:
                raise ToolError("The Scrape.ly documentation index is too large to search.", user=False)
            data = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ToolError(f"The documentation search failed: {exc}", user=False) from exc
    entries = []
    for page in data if isinstance(data, list) else []:
        slug = page.get("slug") if isinstance(page, dict) else None
        if not isinstance(slug, str) or not slug.strip("/"):
            continue
        page_url = f"{cc.settings.docs_url}/{slug.strip('/')}"
        words = slug.replace("-", " ").replace("/", " ")
        for section in page.get("sections") or []:
            if not isinstance(section, dict):
                continue
            anchor = str(section.get("id") or "").strip()
            title = str(section.get("title") or "").strip()
            body = str(section.get("text") or "").strip()
            entries.append(
                {
                    "url": f"{page_url}#{anchor}" if anchor else page_url,
                    "head": f"{words} {title}".lower(),
                    "title": title,
                    "text": body,
                    "lower": body.lower(),
                }
            )
    index_cache.set(url, entries)
    return entries


def _snippet(entry: dict, terms: list[str]) -> str:
    body, lower = entry["text"], entry["lower"]
    hits = [i for i in (lower.find(t) for t in terms) if i >= 0]
    start = max(0, min(hits) - SNIPPET_CHARS // 4) if hits else 0
    if start:
        space = body.rfind(" ", 0, start)
        start = space + 1 if space >= 0 else start
    piece = body[start : start + SNIPPET_CHARS].strip()
    if piece and start + SNIPPET_CHARS < len(body):
        piece += "…"
    if piece and start:
        piece = "…" + piece
    title = entry["title"]
    return f"{title}: {piece}" if title and piece else (title or piece)


async def search_scrapely_docs(cc: CallContext, query: str) -> list[dict]:
    """Keyword search over the Scrape.ly docs index; sections matching more of the words rank first."""
    terms = list(dict.fromkeys(re.findall(r"[a-z0-9][a-z0-9._-]*", query.lower())))
    if not terms:
        return []
    phrase = " ".join(terms)
    scored = []
    for order, entry in enumerate(await scrapely_index(cc)):
        matched = score = 0
        for term in terms:
            in_head = term in entry["head"]
            count = entry["lower"].count(term)
            if in_head or count:
                matched += 1
                score += (6 if in_head else 0) + min(count, 5)
        if not matched:
            continue
        if len(terms) > 1 and (phrase in entry["lower"] or phrase in entry["head"]):
            score += 8
        scored.append((-matched, -score, order, entry))
    scored.sort(key=lambda row: row[:3])
    results = []
    for *_, entry in scored[:HITS_PER_QUERY]:
        item = {"url": entry["url"]}
        snippet = _snippet(entry, terms)
        if snippet:
            item["content"] = snippet
        results.append(item)
    return results


async def search_docs(cc: CallContext, args: dict) -> dict:
    source_id = args.get("docSource") or "scrapely"
    query = str(args.get("query") or "").strip()
    limit = int(args.get("limit") or 5)
    offset = int(args.get("offset") or 0)
    if not query:
        raise ToolError("The search query is empty.")
    source = cc.settings.docs_sources.get(source_id)
    if source_id != "scrapely" and source is None:
        raise ToolError(f"Unknown documentation source '{source_id}'. Use scrapely, crawlee-js or crawlee-py.")
    if source is not None and not source.configured:
        raise ToolError(f"The {source_id} documentation search is not configured on this server.")
    key = f"{source_id}::{query.lower()}"
    results = search_cache.get(key)
    if results is None and source is None:
        results = await search_scrapely_docs(cc, query)
        search_cache.set(key, results)
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


def doc_url_allowed(url: str, docs_url: str) -> bool:
    """https pages on crawlee.dev, or at or under the Scrape.ly docs URL (scrape.ly/docs by default)."""
    try:
        parts = urlsplit(url)
        docs = urlsplit(docs_url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "https" or parts.username or parts.password or "@" in parts.netloc:
        return False
    if (parts.hostname or "").lower() == CRAWLEE_DOC_HOST and port is None:
        return True
    if parts.netloc.lower() != docs.netloc.lower():
        return False
    base = docs.path.rstrip("/")
    path = parts.path.rstrip("/")
    return path == base or path == base + ".md" or path.startswith(base + "/")


def markdown_url(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path.rstrip("/")
    if not path.endswith(".md"):
        path = (path + ".md") if path else "/index.md"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


async def fetch_docs(cc: CallContext, args: dict) -> dict:
    url = str(args.get("url") or "").strip()
    if not doc_url_allowed(url, cc.settings.docs_url):
        raise ToolError(
            f"Only documentation pages under {cc.settings.docs_url} and https://{CRAWLEE_DOC_HOST} can be fetched."
            + (" Find pages with search-scrapely-docs." if cc.has("search-scrapely-docs") else "")
        )
    parts = urlsplit(url)
    md = markdown_url(urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, "")))
    content = page_cache.get(md)
    if content is None:
        try:
            async with httpx.AsyncClient(timeout=FETCH_TIMEOUT, follow_redirects=True) as client:
                resp = await client.get(md, headers={"User-Agent": USER_AGENT})
        except httpx.HTTPError as exc:
            raise ToolError(f"Fetching {url} failed: {exc}", user=False) from exc
        if resp.status_code >= 400:
            raise ToolError(f"Fetching {url} failed with HTTP status {resp.status_code}.", user=False, http_status=resp.status_code)
        content = resp.text
        page_cache.set(md, content)
    return ok({"url": url, "content": content}, [f"Documentation page {url}:\n\n{content}"])
