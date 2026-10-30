# Changelog

All notable changes to this project will be documented in this file.

## [0.3.8] - 2026-10-30

- Prices per plan use the plan names (Free, Starter, Scale, Business) in plan order, never the internal tier codes;
  `tieredPricing` entries add a `plan` field. The price shown is the one the server bills.
- A tool that fails with an internal error no longer returns the exception text to the client.
- Log lines escape control characters (newlines, escape sequences), so a value can't add log lines or reach a terminal.
- Hosted server: sessions without a token share a cap (`SCRAPELY_MCP_MAX_ANONYMOUS_SESSIONS`, default 1000), and the
  per-IP session limit groups IPv6 addresses by /64.
- certifi 2026.10.23.

## [0.3.7] - 2026-10-26

- Session log: tool arguments (including Actor input fields marked `isSecret`), results and `context` are reported as
  they are; nothing is masked. Only sizes are bounded (arguments 64 KB, the result's first 4,000 characters).

## [0.3.6] - 2026-10-26

- Every tool accepts an optional `context` string: the AI says in one or two sentences what the user wants and why it
  calls the tool. The server removes it before validating the arguments, so it never reaches a tool, an Actor or an
  MCP-server Actor, and clients that do not send it work as before. A tool whose own input has a `context` field keeps
  it unchanged. The server instructions ask for it on every call.
- Session log: the server reports each session to the Scrape.ly API (`POST /v2/mcp/session-log`, with the caller's
  token): client name and version from `initialize`, transport, server version, every tool call (arguments, `context`,
  status, duration, result summary with run, dataset and Actor ids and the runs it started, the first 4,000 characters
  of the result), resource reads, lists and the session end. Actor input fields marked `isSecret` are masked before
  sending; the API removes credential-like values before storing. Batches go from a background task and never delay or
  fail a call. `SCRAPELY_MCP_SESSION_LOG=false` turns it off. The hosted server also sends the caller's country
  (from Cloudflare), never the IP.
- Session log reports never loop: a batch is sent once and dropped on failure (no retry). A 401/403 pauses reports
  with that credential for an hour, a 404 (an API server without the endpoint) pauses them for the whole process for an
  hour, and a 429/5xx pauses them for `Retry-After` (at most 10 minutes). Nothing is sent without a credential.

## [0.3.5] - 2026-10-24

- `search-actors` cards list each Actor's input fields: name, type, whether it is required, a short description, the
  default and example (prefill) values and the allowed values (`enum`), from the input schema of the build users run.
  Before, the cards never had input fields: the API ignored `includeInputSchema`. The card text shows one line per
  field within about 1,500 characters (required fields first) and lists the other fields by name; descriptions are
  cut at 100 characters, values over 120 characters and long enums are left out (`enumTotal` says how many values
  there are). Secret fields (`isSecret`) never show a value, hidden fields are left out. The full schema stays in
  `fetch-actor-details`. An example (prefill) equal to the default is not repeated.
- Pay-per-event prices lead with one comparable number: "$2.00 per 1,000 results", with the billed unit as a note when it
  is not called a result ("$0.50 per 1,000 results (charged per Review scraped)"). An event that is not one per result
  (`primaryEventPerResult: false` or the event's `perResult: false`, such as a page of a document or a minute of audio)
  keeps its own unit: "$0.0015 per Scraped page (not per result)". New pricing fields: `chargeUnit`,
  `chargedPerResult`, `usdPer1000Results`, and in search cards `chargeUnitPriceUsd`. Prices and charges are unchanged.
- `userTier` is gone from `search-actors` and from `fetch-actor-details` pricing. It echoed the caller's plan size and
  changed no price. Prices now say "List price, the same on every plan", or, for events with a Store discount, that the
  plan's Store discount comes off when billed. Plan-dependent prices (none in the Store today) say so.
- Platform usage is quoted "at your plan's rates" (compute and proxy rates differ by plan).

## [0.3.4] - 2026-10-24

- `search-actors` (and `search-actors-widget`) rank with the Store's shared ranking (`sortBy=recommended`), the
  same as the dashboard chat: it matches words in titles, descriptions, categories, output types and target sites,
  counts partial matches and a few synonyms ("footage" finds video Actors), and puts Scrape.ly-maintained and tested
  Actors first. Before, every word had to appear in the name, title or description and results were ordered by
  popularity only: "stock footage" found nothing and "logo" put the logo finder third.
- A search that finds nothing tells the agent to search once more and then tell the user that no Scrape.ly Actor
  does this yet, instead of guessing.
- Actor cards always carry the description, developer and categories, whichever `output` sections were asked for
  (they were empty placeholders when only `stats` or `pricing` was requested).
- Actor cards show the public success rate (`stats.successRate` and `stats.successRateWindow`: the share of the
  Actor's finished runs that succeeded, for its current version or the last 30 days; `null` when it has too few
  runs). The rating only appears when the Actor has real reviews. The server instructions say to prefer Actors
  maintained by Scrape.ly with a high success rate, instead of "well rated" ones.
- Actors of official Scrape.ly accounts say "Maintained by Scrape.ly" with the studio's name
  (`developer.isOfficial`, `developer.name`, `developer.maintainedBy`), from the API's `maintainedByScrapely` flag.
  `SCRAPELY_OFFICIAL_USERNAMES` is only used with an API that does not send the flag. Before, no Actor was ever
  marked official, and the texts said Store Actors come from independent developers.
- Long READMEs are shortened by section within about 12,000 characters (was a hard cut at 5,000): long examples and
  tables are shortened first, the pricing, limits and FAQ sections are always kept, and the headings of any sections
  left out are listed. Most Store READMEs now come back whole.
- `fetch-actor-details` with `output.outputSchema` returns the Actor's output schema: the dataset item fields with
  their types, labels and formats (from the dataset schema and its views), the dataset views and the named outputs.
  It said "No output schema is available" for every Actor.
- The widget's `stats.actorReviewRating` and `actorReviewCount` are always numbers.

## [0.3.3] - 2026-10-24

- Actor prices in `search-actors` and `fetch-actor-details` say how platform usage is billed for pay-per-event Actors.
  If an official Actor's price includes the usage (`isPPEPlatformUsagePaidByUser: false`), the price says "platform
  usage included". Every other pay-per-event Actor says "plus platform usage". When the API sends a `usageEstimate`,
  that line adds the typical usage per 1,000 results and the estimated total per 1,000 results. New pricing fields:
  `platformUsageIncluded`, `estUsageUsdPer1000`, `estTotalUsdPer1000`. Before, pay-per-event prices listed only the
  events.
- `search-scrapely-docs` now searches the Scrape.ly docs (the default `docSource: "scrapely"`). It reads the docs
  site's own search index (`https://scrape.ly/docs/search-index.json`) and returns section links with snippets; before,
  it failed with "not configured" unless an Algolia index was set, which no deployment had. The
  `SCRAPELY_DOCS_ALGOLIA_*` variables are gone; `SCRAPELY_DOCS_URL` (default `https://scrape.ly/docs`) points both
  docs tools at another copy of the docs.
- `fetch-scrapely-docs` reads Scrape.ly pages under `https://scrape.ly/docs` (it only allowed `docs.scrape.ly`, a
  host that does not exist). A URL that already ends in `.md` is fetched as it is.
- Task names are limited to 30 characters (`create-actor-task`, `update-actor-task`), as the API enforces; the tools
  said 63.
- `call-actor` says that `memory` is capped by the plan's maximum for one run (a larger value is refused), and
  `get-key-value-store-record` says that records above the read limit (1 MiB by default) also come back as a link.

## [0.3.2] - 2026-10-21

Security patch release for local (stdio) users.

- Security: requests to an MCP-server Actor (which carry the caller's token) only ever go to the Actor's own standby
  path `/v2/{username}/{name}/`. A legacy SSE `endpoint` or a redirect that points anywhere else on the API is refused
  before it is sent, and the call fails at once. Every such request carries `X-Scrapely-Mcp-Remote: 1`, and the API
  serves a request that carries it only from the standby router.
- Process-wide budget for kept Actor tool definitions (64 MiB, `SCRAPELY_MCP_MAX_TOTAL_TOOL_BYTES`): a new session or
  stateless request that does not fit gets a 503 with `Retry-After`; closing a session gives its bytes back.
- Process-wide budget for data arriving from MCP-server Actors (64 MiB, `SCRAPELY_MCP_MAX_REMOTE_INFLIGHT_BYTES`),
  counted per chunk as it arrives; task results stay counted until the task store has taken them over.
- Each selected Actor is loaded once per session, whatever its spelling (quotes, `~`, `-slash-`) or whether it is
  named by id, and the selected-Actor limit applies while loading.
- Tool definitions from Actors and their MCP servers are byte-bounded per Actor and per session; remote titles and
  descriptions are truncated, only standard annotations are kept, oversized output schemas are dropped, and invalid
  or repeated remote tool names are skipped.
- One tool result is capped at 4 MiB (`SCRAPELY_MCP_MAX_TOOL_RESULT_BYTES`); a larger one is returned as an error.
- Security: argument validation is bounded. `uniqueItems` is checked in linear time (jsonschema compared every pair
  of items when they could not be sorted, e.g. objects), every validation has a time budget (2 s,
  `SCRAPELY_MCP_VALIDATION_BUDGET_SECS`) after which the call is refused as too large or complex, and validation runs
  in a worker thread instead of on the event loop.
- Protocol task results are byte-bounded per result, per session and in total, released 60 s after they are fetched,
  and finished tasks are kept for 15 minutes instead of 24 hours. Closing a session drops its tasks and cancels the
  running ones.
- Security: dataset and key-value store tools read API responses as a stream and stop at a byte limit (1 MiB,
  `SCRAPELY_MCP_MAX_DATASET_PAGE_BYTES` / `SCRAPELY_MCP_MAX_RECORD_BYTES`) instead of buffering whatever the request
  selected. `get-dataset-items` and `get-dataset-schema` take at most 1000 items (`limit` maximum); a page of large
  items is cut after its last complete item and says where to continue; a larger record is returned as a download
  link.

## [0.3.1] - 2026-10-20

- Add a `NOTICE` file with the third-party license notice for the tool JSON schemas, and ship it (and
  `PROVENANCE.md`) in the sdist and wheel.

## [0.3.0] - 2026-10-20

- New default `automations` category: `list-saved-inputs` (an Actor's saved inputs and public example inputs, or all
  saved inputs in the account), `run-saved-input` (start a saved input, optionally changing some fields),
  `list-automations` (filter by Actor or saved input) and `run-automation` (run an automation now).

## [0.2.1] - 2026-10-19

- Initial public release.
