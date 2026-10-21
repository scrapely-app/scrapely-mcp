# Changelog

All notable changes to this project will be documented in this file.

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
