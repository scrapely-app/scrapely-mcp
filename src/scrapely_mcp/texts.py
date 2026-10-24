"""User-facing tool titles and descriptions.

Descriptions only mention other tools when those tools are part of the session,
so each description is a function of the session's tool names and mode.
"""

from __future__ import annotations

from collections.abc import Callable

Names = set[str]


def _if(names: Names, tool: str, text: str) -> str:
    return text if tool in names else ""


TITLES = {
    "search-actors": "Search Actors",
    "fetch-actor-details": "Actor details",
    "call-actor": "Run an Actor",
    "get-actor-run": "Run status",
    "get-actor-run-list": "List runs",
    "get-actor-log": "Run log",
    "abort-actor-run": "Abort run",
    "get-dataset": "Dataset info",
    "get-actor-output": "Run output",
    "get-dataset-items": "Dataset items",
    "get-dataset-schema": "Dataset schema",
    "get-dataset-list": "List datasets",
    "get-key-value-store": "Key-value store info",
    "get-key-value-store-keys": "Key-value store keys",
    "get-key-value-store-record": "Key-value store record",
    "get-key-value-store-list": "List key-value stores",
    "create-actor-task": "Create task",
    "get-actor-task": "Get task",
    "update-actor-task": "Update task",
    "publish-actor-task": "Publish task",
    "unpublish-actor-task": "Unpublish task",
    "list-saved-inputs": "List saved inputs",
    "run-saved-input": "Run a saved input",
    "list-automations": "List automations",
    "run-automation": "Run an automation now",
    "search-scrapely-docs": "Search docs",
    "fetch-scrapely-docs": "Read docs page",
    "report-problem": "Report a problem",
    "search-actors-widget": "Search Actors (widget)",
    "fetch-actor-details-widget": "Actor details (widget)",
    "call-actor-widget": "Run an Actor (widget)",
    "get-actor-run-widget": "Run status (widget)",
}


def search_actors(n: Names, mode: str) -> str:
    return (
        "Look up Actors in the Scrape.ly Store by keyword. This only finds Actors: it does not run anything "
        "and does not fetch any data. Store Actors are scrapers, crawlers, agents and MCP servers published "
        "by independent developers for legitimate use.\n\n"
        "Keywords: use one to three plain words, ideally the platform name plus the kind of data "
        "(for example 'instagram posts' or 'google maps'). If the user named no platform or data type, pass "
        "an empty string to get the Store's default ordering, which is by popularity. Words such as 'best' "
        "or 'top' and bare task verbs like 'scrape' do not help the search.\n\n"
        "Run at least two searches: start broad (just the platform name), then narrow down. Each result is "
        "a short summary that lists the Actor's input fields with their types; `name?: type` marks an "
        "optional field. Only public Actors are searched."
        + _if(n, "fetch-actor-details", " Use fetch-actor-details for the full README and input schema.")
        + " When the user simply wants data right now, pick a general-purpose scraping Actor and run it."
    )


def fetch_actor_details(n: Names, mode: str) -> str:
    return (
        "Get information about one Actor by its exact ID or full name (`username/actor-name`). "
        "Do not guess names"
        + (", find them with search-actors first" if "search-actors" in n else "")
        + ".\n\nThe `output` object picks what to return, which keeps responses small: description, stats, "
        "pricing, rating, metadata, inputSchema, readme, outputSchema and mcpTools. Without `output`, "
        "everything except mcpTools (and the output schema) is returned. `readme` returns the Actor's README "
        "summary when one exists, otherwise the full README. Set `mcpTools: true` to list the tools of an "
        "MCP-server Actor."
    )


def call_actor(n: Names, mode: str) -> str:
    parts = [
        "Start any Actor from the Store with a JSON `input` and optionally wait for it to finish.",
        (
            "Before calling, read the Actor's input schema with fetch-actor-details."
            if "fetch-actor-details" in n
            else "Make sure `input` matches the Actor's input schema."
        ),
    ]
    if "search-actors" in n:
        parts.append("If you only have a loose name rather than `username/actor-name`, resolve it with search-actors.")
    parts.append(
        "MCP-server Actors are called as `actorName:toolName`"
        + (" (list their tools with fetch-actor-details and `output.mcpTools`)." if "fetch-actor-details" in n else ".")
    )
    parts.append(
        "`waitSecs` (0 to 45, default 30) is how long to wait for the run. Use 0 for long runs: the call then "
        "returns right after the start. The result holds the run status and storage IDs, never the dataset rows"
        + (
            "; fetch the results with get-actor-output."
            if "get-actor-output" in n
            else ("; read rows with get-dataset-items." if "get-dataset-items" in n else ".")
        )
        + " If the run is still going, the result says how to poll it."
    )
    parts.append("When a dedicated tool exists for an Actor, prefer it over this tool.")
    parts.append(
        "callOptions: `build` only applies to your own Actors (other Actors always run their publisher's "
        "build); `memory` is in MB, from 128 up to your plan's maximum for one run (at most 32768; a larger value is "
        "refused); `timeout` is in seconds (0 means no limit). `maxItems` "
        "caps the billed results of pay-per-result Actors and `maxTotalChargeUsd` caps the total charge of "
        "pay-per-event Actors; neither stops the Actor from working, so prefer the Actor's own limit fields. "
        "Your plan also limits the total memory of concurrent runs."
    )
    if mode == "apps":
        parts.append(
            "If the user wants to watch the run's progress, use call-actor-widget instead. For silent name "
            "lookups use search-actors, not its widget."
        )
    return "\n\n".join(parts)


def get_actor_run(n: Names, mode: str) -> str:
    return (
        "Report the state of a run: status, a map of its storages (datasets and key-value stores by alias), "
        "stats, a summary and the recommended next step. `waitSecs` (0 to 45, default 30) waits for the run "
        "to reach a terminal state; 0 returns the current state at once. Works for runs started by any tool."
    )


def get_actor_run_list(n: Names, mode: str) -> str:
    return (
        "List your Actor runs with their default storage IDs, up to 10 per call. Filter with `status`: READY "
        "(starting), RUNNING, SUCCEEDED, FAILED, TIMING-OUT, TIMED-OUT, ABORTING, ABORTED. Runs are sorted by "
        "start time, oldest first, unless `desc` is true."
    )


def get_actor_log(n: Names, mode: str) -> str:
    return "Return the last `lines` lines of a run's log (default 10, at most 50; 0 returns the whole log)."


def abort_actor_run(n: Names, mode: str) -> str:
    return (
        "Abort a running run. With `gracefully: true` the Actor gets a short time to save its state "
        "before it stops."
    )


def get_dataset(n: Names, mode: str) -> str:
    tools = [t for t in ("get-dataset-items", "get-dataset-schema") if t in n]
    hint = ""
    if "get-dataset-items" in n:
        hint += " Use get-dataset-items for the rows."
    if "get-dataset-schema" in n:
        hint += " Use get-dataset-schema for inferred field types."
    del tools
    return (
        "Return metadata of one dataset: its field list, item count and size, not its rows. Do not use it when "
        "the user wants the data itself." + hint + " `stats.inflatedBytes` approximates the uncompressed size. "
        "Item counts can lag by about 5 seconds after a run."
    )


def get_actor_output(n: Names, mode: str) -> str:
    return (
        "Fetch the results of a run in one call: a page of its dataset items and, when available, its OUTPUT "
        "record. Pass `runId` (its default dataset and key-value store are used) or a `datasetId`. Page with "
        "`offset` and `limit` (default 100, at most 1000) and pick columns with `fields` (comma-separated, dot "
        "notation for nested fields). To keep the reply small, long text values are cut and a page may hold "
        "fewer items than `limit`; `hasMore` and `nextOffset` say where to continue. Works for any run you can "
        "read with your token."
    )


def get_dataset_items(n: Names, mode: str) -> str:
    return (
        "Read rows from a dataset. Page with `offset` and `limit` (default 20, at most 1000); a page of large "
        "items may hold fewer items than `limit`, and the reply says where to continue. `fields` and `omit` take "
        "comma-separated field names (dot notation selects nested fields), `flatten` takes comma-separated "
        "fields to flatten, `desc` returns newest first and `clean` skips empty items and hidden fields. "
        "Accepts a dataset ID or `username~dataset-name`."
    )


def get_dataset_schema(n: Names, mode: str) -> str:
    return "Infer a JSON schema from a sample of a dataset's items (default 5 items, at most 1000)."


def get_dataset_list(n: Names, mode: str) -> str:
    return (
        "List your datasets, up to 20 per call, sorted by creation time. Datasets created by runs have no name; "
        "include them with `unnamed: true`. `stats.inflatedBytes` gives an idea of each dataset's size."
    )


def get_key_value_store(n: Names, mode: str) -> str:
    return "Return metadata of one key-value store. Accepts a store ID or `username~store-name`."


def get_key_value_store_keys(n: Names, mode: str) -> str:
    return (
        "List keys in a key-value store, up to 10 per call. Continue with `exclusiveStartKey` set to the last "
        "key of the previous page."
    )


def get_key_value_store_record(n: Names, mode: str) -> str:
    return (
        "Read one record of a key-value store by its exact key (this does not list keys). JSON, text and "
        "binary records are supported; binary records above 256 KiB, and records too large to read here (above 1 MiB "
        "by default), are returned as a download link."
    )


def get_key_value_store_list(n: Names, mode: str) -> str:
    return (
        "List your key-value stores, up to 10 per call, sorted by creation time. Stores created by runs have no "
        "name; include them with `unnamed: true`."
    )


_TASK_NAME_RULE = "Task names are 3 to 30 letters, digits or dashes and cannot start or end with a dash."


def create_actor_task(n: Names, mode: str) -> str:
    parts = [
        "Save an Actor's input and run options as a task, so it can be reused from the dashboard, schedules or "
        "the API. Creating a task does not publish it.",
    ]
    pub = "`publicConfig` (SEO title and description) can be set now"
    if "update-actor-task" in n:
        pub += " or later with update-actor-task"
    pub += "; the task stays private"
    if "publish-actor-task" in n:
        pub += " until you publish it with publish-actor-task"
    parts.append(pub + ".")
    parts.append(
        "If the user's intent is clear, choose sensible input values from the input schema and say which ones "
        "you picked."
        + (" Resolve loosely named Actors with search-actors." if "search-actors" in n else "")
        + (" Read the input schema with fetch-actor-details." if "fetch-actor-details" in n else "")
    )
    parts.append("`build` only applies to your own Actors. " + _TASK_NAME_RULE)
    return "\n\n".join(parts)


def get_actor_task(n: Names, mode: str) -> str:
    return (
        "Read a saved task: its Actor, name, title, input (secret fields stay encrypted), options and "
        "publication state." + _if(n, "update-actor-task", " Change it with update-actor-task.")
    )


def update_actor_task(n: Names, mode: str) -> str:
    pub = [t for t in ("publish-actor-task", "unpublish-actor-task") if t in n]
    return (
        "Change a task's input, run options or public page settings. This does not publish or unpublish the "
        "task" + (f" (use {' / '.join(pub)} for that)" if pub else "") + ". A new `input` replaces the stored "
        "input completely. `publicConfig` fields are stored as the task's SEO title and description. `build` "
        "only applies to your own Actors. " + _TASK_NAME_RULE
    )


def publish_actor_task(n: Names, mode: str) -> str:
    return (
        "Publish a task as a public landing page for one specific use case of its Actor: what it does, the "
        "chosen inputs and the expected output. Published tasks appear among the Actor's examples and can be "
        "found by users, search engines and agents, so publish only useful, reliable and specific tasks. The "
        "Actor must be public, and an Actor can have at most 50 published tasks. Publishing an already "
        "published task changes nothing. Requires write access to the task and the Actor. If publishing "
        "fails, follow the reason the API gives and only change fields the reason names."
    )


def unpublish_actor_task(n: Names, mode: str) -> str:
    return (
        "Take a published task's public page down. Its display settings are kept, so it can be published "
        "again later. Unpublishing a task that is not published does nothing. Requires write access."
    )


def search_docs(n: Names, mode: str) -> str:
    return (
        "Full-text search in documentation. Pick the source with `docSource`:\n"
        "- `scrapely`: the Scrape.ly platform docs (Actors, API, storage, proxy, CLI)\n"
        "- `crawlee-js`: Crawlee for JavaScript, the web scraping library\n"
        "- `crawlee-py`: Crawlee for Python\n"
        "Search with keywords, not full sentences. Results are page URLs with short snippets"
        + _if(n, "fetch-scrapely-docs", "; read a full page with fetch-scrapely-docs")
        + ". Do not search the Store here; that is only for finding Actors when the user asks for them."
    )


def fetch_docs(n: Names, mode: str) -> str:
    return (
        "Fetch the full text of a documentation page as Markdown. Only https URLs of Scrape.ly docs pages "
        "(https://scrape.ly/docs/...) and crawlee.dev pages are allowed."
    )


def report_problem(n: Names, mode: str) -> str:
    return (
        "Send a short problem report to the Scrape.ly team when a tool or Actor does not work: it is missing, "
        "fails, times out, returns confusing or empty results, or the request cannot be completed. Describe "
        "what you were doing and what went wrong. Do not include personal data, credentials or the user's "
        "conversation verbatim."
    )


def search_widget(n: Names, mode: str) -> str:
    return (
        "Search the Store and show the results to the user as an interactive card list. Use it only when the "
        "user wants to see the results; for silent lookups use search-actors."
    )


def details_widget(n: Names, mode: str) -> str:
    return (
        "Show one Actor's details to the user as an interactive card. Use it only when the user wants to see "
        "the Actor; for silent lookups use fetch-actor-details."
    )


def call_widget(n: Names, mode: str) -> str:
    return (
        "Start an Actor and show its progress to the user in a live widget. The call returns immediately; the "
        "widget follows the run on its own, so do not poll this run afterwards. MCP-server tools "
        "(`actorName:toolName`) cannot be shown as widgets; use call-actor for them."
    )


def run_widget(n: Names, mode: str) -> str:
    return (
        "Show a run's status to the user in a live widget. The widget keeps itself up to date, so do not poll "
        "the run again after it renders."
    )


def list_saved_inputs(n: Names, mode: str) -> str:
    return (
        "List the user's saved inputs: named, reusable inputs for an Actor (stored as Actor tasks). Pass actorId "
        "to see one Actor's saved inputs, its default first, plus the Actor's public example inputs; without it, "
        "all saved inputs in the account are listed."
        + _if(n, "run-saved-input", " Start one with run-saved-input.")
    )


def run_saved_input(n: Names, mode: str) -> str:
    return (
        "Start a run of one of the user's saved inputs (an Actor task). The run uses the saved input and its run "
        "options; `input` changes only the fields it names (shallow merge over the saved input). The run is billed "
        "like any other run. By default the call returns right after the start; pass waitSecs to wait up to 45 "
        "seconds."
        + _if(n, "get-actor-run", " Follow the run with get-actor-run.")
    )


def list_automations(n: Names, mode: str) -> str:
    return (
        "List the user's automations. An automation has a trigger (manual, a schedule, or another run "
        "finishing), one or more run steps, optional filters, an optional AI step and deliveries such as email "
        "or Slack. Filter by actorId or savedInputId to find the automations that use them."
        + _if(n, "run-automation", " Start one now with run-automation.")
    )


def run_automation(n: Names, mode: str) -> str:
    return (
        "Run one of the user's automations now, whatever its trigger. Its steps start Actor runs, which are "
        "billed like any run; an AI step, if the automation has one, is billed like the AI chat. Returns the "
        "automation run with the started Actor runs."
    )


DESCRIPTIONS: dict[str, Callable[[Names, str], str]] = {
    "search-actors": search_actors,
    "fetch-actor-details": fetch_actor_details,
    "call-actor": call_actor,
    "get-actor-run": get_actor_run,
    "get-actor-run-list": get_actor_run_list,
    "get-actor-log": get_actor_log,
    "abort-actor-run": abort_actor_run,
    "get-dataset": get_dataset,
    "get-actor-output": get_actor_output,
    "get-dataset-items": get_dataset_items,
    "get-dataset-schema": get_dataset_schema,
    "get-dataset-list": get_dataset_list,
    "get-key-value-store": get_key_value_store,
    "get-key-value-store-keys": get_key_value_store_keys,
    "get-key-value-store-record": get_key_value_store_record,
    "get-key-value-store-list": get_key_value_store_list,
    "create-actor-task": create_actor_task,
    "get-actor-task": get_actor_task,
    "update-actor-task": update_actor_task,
    "publish-actor-task": publish_actor_task,
    "unpublish-actor-task": unpublish_actor_task,
    "list-saved-inputs": list_saved_inputs,
    "run-saved-input": run_saved_input,
    "list-automations": list_automations,
    "run-automation": run_automation,
    "search-scrapely-docs": search_docs,
    "fetch-scrapely-docs": fetch_docs,
    "report-problem": report_problem,
    "search-actors-widget": search_widget,
    "fetch-actor-details-widget": details_widget,
    "call-actor-widget": call_widget,
    "get-actor-run-widget": run_widget,
}

WIDGET_NO_POLL = (
    "The widget shows the run's live progress to the user. Do not poll this run; wait for the user to ask."
)


def instructions(names: Names, mode: str, public_api_base: str, report_problem_available: bool) -> str:
    api = public_api_base.rstrip("/")
    parts = [
        "Scrape.ly runs Actors: serverless cloud programs for web scraping, data extraction and automation. "
        "An Actor takes JSON input and produces output.",
        "Before running an Actor, read its README and input schema with fetch-actor-details.",
        "Search the Scrape.ly Store first: a suitable Actor may already exist. When several fit, prefer ones "
        "that are widely used and well rated. Store Actors are published by independent developers for "
        "legitimate use.",
        "Every run has a default dataset and a default key-value store, which may be empty. Runs can take a "
        "while and their output can be large, so page through datasets instead of reading everything at once.",
        "Tasks are saved Actor configurations (input plus build, memory and timeout). Creating or updating a "
        "task never publishes it. Publishing creates a public page for one use case; reserve it for useful, "
        "reliable use cases.",
        f"Any Scrape.ly API GET endpoint can be read with resources/read using a full URL such as "
        f"{api}/v2/datasets/<datasetId>/items?limit=20. The server adds your token. Tool results return IDs, "
        "so build the URL from them. Bodies above about 256 KB come back as a download link instead. Resource "
        "reads need a token (sessions that only use agentic payment cannot read them). resources/templates/list "
        "shows common URL shapes.",
        "call-actor: get the input schema first. MCP-server Actors are called as `actorName:toolName`. "
        "`waitSecs` defaults to 30 and is at most 45; 0 starts the run and returns at once. A positive value "
        "waits and returns the status and storage IDs, never the rows: fetch the results with get-actor-output "
        "(or get-dataset-items for finer control).",
        "search-actors finds Actors; fetch-actor-details describes one. When a dedicated Actor tool exists, "
        "prefer it over call-actor.",
    ]
    if mode == "apps":
        parts.append(
            "Widgets: search-actors-widget, fetch-actor-details-widget, call-actor-widget and "
            "get-actor-run-widget render interactive UI for the user, while the plain tools are silent data "
            "lookups. Use a widget only when the user asks to see results or progress. After a widget renders a "
            "run, do not poll that run again. Polling get-actor-run after a plain call-actor is fine."
        )
    if report_problem_available:
        parts.append(
            "If a failure cannot be resolved, report it with report-problem before telling the user it failed."
        )
    return "\n\n".join(parts)
