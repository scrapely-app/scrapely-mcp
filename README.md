# scrapely-mcp

An [MCP](https://modelcontextprotocol.io) server for [Scrape.ly](https://scrape.ly). It gives AI agents and MCP clients
access to a Scrape.ly account: search the Store for Actors, read their README and input schema, run them, follow the
runs, and read the results from datasets, key-value stores and logs. It can also manage saved tasks and search the
Scrape.ly and Crawlee documentation.

The same package runs in two ways:

- **Hosted**: `scrapely-mcp-http`, a multi-tenant streamable HTTP server. Scrape.ly runs it behind the API at
  `https://api.scrape.ly/mcp`. Most users connect to that URL and never install anything.
- **Local**: `scrapely-mcp-server`, a stdio server started by a desktop MCP client on your machine.

## Connect to the hosted server

Point your client at `https://api.scrape.ly/mcp`.

- Clients that support MCP authorization find the OAuth flow on their own and ask you to sign in.
- Otherwise send your API token as a header: `Authorization: Bearer <token>`.
- Clients that cannot set headers can append `?token=<token>` to the URL. URLs tend to end up in logs, so prefer
  the header.

Example for a client that takes a JSON config:

```json
{
  "mcpServers": {
    "scrapely": {
      "url": "https://api.scrape.ly/mcp",
      "headers": { "Authorization": "Bearer YOUR_TOKEN" }
    }
  }
}
```

## Run locally (stdio)

Requires Python 3.11 or newer.

```bash
uvx --from scrapely-mcp scrapely-mcp-server
# or
pip install scrapely-mcp
scrapely-mcp-server
```

The token comes from `SCRAPELY_TOKEN`, or from the `token` field of `~/.scrapely/credentials.json` or
`~/.scrapely/auth.json`. A token is required unless every selected tool works without one
(`search-actors`, `fetch-actor-details` and the docs tools).

```json
{
  "mcpServers": {
    "scrapely": {
      "command": "uvx",
      "args": ["--from", "scrapely-mcp", "scrapely-mcp-server"],
      "env": { "SCRAPELY_TOKEN": "YOUR_TOKEN" }
    }
  }
}
```

Options:

| Flag | Meaning |
| --- | --- |
| `--tools a,b` | Tool categories or tool names to enable. Replaces the default set. |
| `--actors user/actor,...` | Expose these Actors as their own tools. |
| `--ui auto\|apps\|default` | MCP Apps widget mode (widgets are disabled in this release, see below). |
| `--telemetry-enabled` | Opt in to usage telemetry (also needs a write key, see below). |
| `--telemetry-env PROD\|DEV` | Which telemetry write key variable to use. |

## Tools

Default categories: `actors`, `docs`, `runs`, `storage`, `tasks`. The `dev` category (`report-problem`) is off by
default.

| Category | Tools |
| --- | --- |
| actors | `search-actors`, `fetch-actor-details`, `call-actor` |
| docs | `search-scrapely-docs`, `fetch-scrapely-docs` |
| runs | `get-actor-run`, `get-actor-output`, `get-actor-run-list`, `get-actor-log`, `abort-actor-run` |
| storage | `get-actor-output`, `get-dataset`, `get-dataset-items`, `get-dataset-schema`, `get-key-value-store`, `get-key-value-store-keys`, `get-key-value-store-record`, `get-dataset-list`, `get-key-value-store-list` |
| tasks | `create-actor-task`, `get-actor-task`, `update-actor-task`, `publish-actor-task`, `unpublish-actor-task` |
| dev | `report-problem` |

Whenever `call-actor`, `get-actor-run` or an Actor tool is enabled, `get-actor-run`, `get-actor-output`,
`get-dataset-items`, `get-key-value-store-record` and `abort-actor-run` are added as well.

`call-actor` waits up to `waitSecs` (0 to 45, default 30) and returns the run status and storage IDs; the agent then
fetches the results with `get-actor-output` (pass the `runId`, or a `datasetId`). It returns a page of dataset items
(`offset`, `limit` up to 1000, default 100, and `fields` with dot notation) plus the run's `OUTPUT` record. Long text
values are cut and a page may hold fewer items than `limit` so the reply stays small; `hasMore` and `nextOffset` say
where to continue. `get-dataset-items` remains available for finer control. Clients that support protocol tasks can run `call-actor` as a long-running task
and collect the result when the run ends. Progress notifications are sent while a run is awaited, and cancelling the
request aborts the run.

Any Scrape.ly API `GET` endpoint can also be read with `resources/read` using its full URL, for example
`https://api.scrape.ly/v2/datasets/<id>/items?limit=20`.

## Hosted server

```bash
scrapely-mcp-http --host 0.0.0.0 --port 8000
```

| Path | Purpose |
| --- | --- |
| `/mcp` (and any other path) | MCP endpoint (streamable HTTP; 2026-07-28 stateless requests are also served) |
| `/.well-known/oauth-protected-resource` | OAuth protected-resource metadata (also under `/mcp` suffix) |
| `/healthz` | Health check, `{"status":"ok"}` |

URL parameters on the MCP endpoint: `tools`, `actors`, `maxActorMemoryBytes`, `ui`, `payment` (`skyfire` or
`x402`), `telemetry-enabled`, `token`.

Each session gets its own server instance bound to the SHA-256 fingerprint of the credential that opened it; a
request with a different credential is answered as if the session did not exist. `SCRAPELY_TOKEN` is never used by
the HTTP server.

A container image definition is in `Dockerfile` (`docker build --build-arg SOURCE=local -t scrapely-mcp .`).

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `SCRAPELY_TOKEN` | none | Token for the stdio server |
| `SCRAPELY_API_BASE_URL` | `https://api.scrape.ly` | API origin used for outbound calls |
| `SCRAPELY_PUBLIC_API_BASE_URL` | derived | API origin shown to clients and used as OAuth issuer |
| `SCRAPELY_STORE_URL` | `https://scrape.ly/store` | Base of Store links |
| `SCRAPELY_STANDBY_BASE_URL` | API origin | Base for MCP-server Actors |
| `SCRAPELY_MCP_HOST` / `SCRAPELY_MCP_PORT` | `0.0.0.0` / `8000` | HTTP bind address |
| `SCRAPELY_ACTORS` / `SCRAPELY_TOOLS` | none | Default tool selection of the HTTP server |
| `SCRAPELY_MCP_UI` / `UI_MODE` | `auto` | Widget mode |
| `SCRAPELY_MCP_SESSION_IDLE_TIMEOUT_SECS` | `1800` | Idle session timeout |
| `SCRAPELY_MCP_MAX_SESSIONS` | `10000` | Session limit |
| `SCRAPELY_MCP_REPORT_PROBLEM_BLOCKED_CLIENTS` | `anthropic,claude` | Clients that do not see `report-problem` |
| `SCRAPELY_OFFICIAL_USERNAMES` | `scrapely` | Usernames shown as official |
| `SCRAPELY_DOCS_ALGOLIA_APP_ID` / `_API_KEY` / `_INDEX` | empty | Scrape.ly docs search index |
| `SCRAPELY_DOCS_CRAWLEE_JS_*`, `SCRAPELY_DOCS_CRAWLEE_PY_*` | public Crawlee DocSearch values | Crawlee docs search |
| `SCRAPELY_CONSOLE_BASE_URL` / `SCRAPELY_CONSOLE_TOKEN_PREFIX` | disabled | Personalised dashboard links |
| `SCRAPELY_MCP_WIDGETS_ENABLED` / `SCRAPELY_MCP_WIDGET_ASSETS_DIR` | off | MCP Apps widgets (experimental) |
| `SCRAPELY_SEND_LEGACY_PAYMENT_HEADER` | `true` | Also send the legacy payment-protocol header |
| `SKYFIRE_SELLER_SERVICE_ID` | none | Shown in Skyfire payment hints |
| `TELEMETRY_ENABLED`, `SCRAPELY_TELEMETRY_WRITE_KEY`, `SCRAPELY_TELEMETRY_ENDPOINT` | off | Opt-in telemetry |
| `SENTRY_DSN` | none | Error reporting (install the `sentry` extra) |
| `LOG_LEVEL` | `info` | Log level (logs go to stderr) |

Telemetry is off unless it is enabled **and** a write key is configured; the package ships no keys. Logs never
contain tokens, payment tokens or payment signatures.

### Widgets

MCP Apps widget tools (`search-actors-widget`, `fetch-actor-details-widget`, `call-actor-widget`,
`get-actor-run-widget`) are implemented but disabled by default, and no widget HTML is bundled. With widgets
disabled every client gets the plain tool set.

## Development

```bash
uv venv && uv pip install -e ".[test]"
pytest
```

## License

MIT, see [LICENSE](LICENSE).
