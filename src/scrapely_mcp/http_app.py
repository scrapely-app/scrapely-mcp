"""Hosted multi-tenant HTTP gateway (SPEC 4, 11).

A plain ASGI application. Every MCP session owns its own server object and its
own stateful streamable-HTTP session manager; nothing is shared between callers
except process-wide caches keyed by credential.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

import anyio
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

from .config import Settings, is_internal_host, load_settings, parse_bool
from .log import log, register_secret, token_fingerprint
from .payments import make_provider, parse_payment
from .selection import Selection, ToolCapacityError, actor_selectors, load_tools, normalize_selection
from .server import ScrapelyServer, build_state
from .task_store import TASK_METHODS, TaskError, store as task_store

MAX_BODY = 4 * 1024 * 1024
PLACEHOLDER_TOKEN = "your-scrapely-token"


@dataclass
class SessionEntry:
    session_id: str
    token_hash: str
    server: ScrapelyServer
    manager: StreamableHTTPSessionManager
    stop: anyio.Event
    inflight: int = 0
    last_seen: float = field(default_factory=time.monotonic)
    # Admission keys ("cred:<fingerprint>", "ip:<address>") this session counts against.
    keys: tuple[str, ...] = ()


def _json_response_messages(status: int, body: Any, headers: dict[str, str] | None = None):
    raw = json.dumps(body).encode()
    hdrs = [(b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode())]
    for k, v in (headers or {}).items():
        hdrs.append((k.lower().encode(), v.encode()))
    return [
        {"type": "http.response.start", "status": status, "headers": hdrs},
        {"type": "http.response.body", "body": raw},
    ]


def rpc_error(code: int, message: str, rid: Any = None) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


class Gateway:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        default_actors: Any = None,
        default_tools: Any = None,
        ui_option: str = "auto",
        telemetry_enabled: bool | None = None,
    ) -> None:
        self.settings = settings or load_settings()
        self.default_selection = normalize_selection(default_actors, default_tools)
        self.ui_option = ui_option
        self.telemetry_enabled = self.settings.telemetry_enabled if telemetry_enabled is None else telemetry_enabled
        self.sessions: dict[str, SessionEntry] = {}
        self.reserving = 0
        # Open session ids and pending reservations per admission key.
        self._by_key: dict[str, set[str]] = {}
        self._reserved_by_key: dict[str, int] = {}
        self._tg: anyio.abc.TaskGroup | None = None
        self._preloaded: list | None = None
        self._lock = anyio.Lock()

    # -- lifecycle ---------------------------------------------------------------

    async def startup(self) -> None:
        from .core import make_api

        api = make_api(self.settings, None)
        try:
            self._preloaded = await load_tools(self.settings, self.default_selection, api)
        finally:
            await api.aclose()

    async def _sweeper(self) -> None:
        timeout = self.settings.session_idle_timeout
        interval = min(60.0, timeout)
        while True:
            await anyio.sleep(interval)
            self.sweep()
            try:
                task_store.sweep()
            except Exception:  # noqa: BLE001
                log.error("task store sweep failed", exc=True)

    def sweep(self) -> int:
        timeout = self.settings.session_idle_timeout
        now = time.monotonic()
        stale = [s for s in self.sessions.values() if s.inflight == 0 and now - s.last_seen > timeout]
        for entry in stale:
            self.close_session(entry.session_id)
        if stale:
            log.info("closed idle sessions", closed=len(stale), open=len(self.sessions))
        return len(stale)

    def close_session(self, session_id: str) -> None:
        entry = self.sessions.pop(session_id, None)
        if entry is not None:
            for key in entry.keys:
                ids = self._by_key.get(key)
                if ids is not None:
                    ids.discard(session_id)
                    if not ids:
                        self._by_key.pop(key, None)
            # The session's tasks go with it: running ones are cancelled and kept
            # results released, so the session caps also bound task state.
            try:
                task_store.drop_scope(session_id)
            except Exception:  # noqa: BLE001
                log.error("dropping a closed session's tasks failed", exc=True)
            entry.server.state.release_tools()
            entry.stop.set()

    def _register(self, entry: SessionEntry) -> None:
        self.sessions[entry.session_id] = entry
        for key in entry.keys:
            self._by_key.setdefault(key, set()).add(entry.session_id)

    async def __call__(self, scope: dict, receive, send) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope["type"] != "http":
            return
        try:
            await self._http(scope, receive, send)
        except Exception:  # noqa: BLE001
            log.error("unhandled error in HTTP gateway", exc=True)
            try:
                for m in _json_response_messages(500, rpc_error(-32603, "Internal server error.")):
                    await send(m)
            except Exception:  # noqa: BLE001
                pass

    async def _lifespan(self, receive, send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                try:
                    await self.startup()
                    self._tg = anyio.create_task_group()
                    await self._tg.__aenter__()
                    self._tg.start_soon(self._sweeper)
                except Exception as exc:  # noqa: BLE001
                    await send({"type": "lifespan.startup.failed", "message": str(exc)})
                    return
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                for sid in list(self.sessions):
                    self.close_session(sid)
                if self._tg is not None:
                    self._tg.cancel_scope.cancel()
                    try:
                        await self._tg.__aexit__(None, None, None)
                    except BaseException:  # noqa: BLE001
                        pass
                    self._tg = None
                from . import telemetry

                await telemetry.flush()
                await send({"type": "lifespan.shutdown.complete"})
                return

    # -- helpers -------------------------------------------------------------------

    @staticmethod
    def _headers(scope: dict) -> dict[str, str]:
        out: dict[str, str] = {}
        for k, v in scope.get("headers") or []:
            key = k.decode("latin-1").lower()
            val = v.decode("latin-1")
            out[key] = val if key not in out else out[key] + "," + val
        return out

    def _base(self, headers: dict[str, str]) -> str:
        proto = (headers.get("x-forwarded-proto") or "http").split(",")[0].strip() or "http"
        host = headers.get("host") or "localhost:8000"
        return f"{proto}://{host}"

    def _unauthorized(self, headers: dict[str, str]) -> list:
        base = self._base(headers)
        body = rpc_error(
            -32001,
            "A Scrape.ly API token is required. Send it as `Authorization: Bearer <token>`, or complete OAuth "
            "discovery using the resource metadata URL in the WWW-Authenticate header.",
        )
        return _json_response_messages(
            401,
            body,
            {"WWW-Authenticate": f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"'},
        )

    def _metadata(self, headers: dict[str, str]) -> dict:
        from urllib.parse import urlsplit

        from .config import compute_issuer

        issuer = compute_issuer()
        origin = self._base(headers)
        parts = urlsplit(origin)
        if parts.scheme != "https" or is_internal_host(parts.hostname):
            origin = issuer
        return {"resource": origin, "authorization_servers": [issuer], "scopes_supported": ["read", "write"]}

    @staticmethod
    def client_ip(scope: dict, headers: dict[str, str]) -> str:
        """The caller's IP address.

        The gateway is reachable only from the API server's /mcp proxy, which
        replaces X-Forwarded-For with the single client address it resolved.
        Only that last hop is trusted; without the header the socket peer is used.
        """
        xff = headers.get("x-forwarded-for")
        if xff:
            last = xff.split(",")[-1].strip()
            if last:
                return last[:64]
        client = scope.get("client")
        if client:
            return str(client[0])
        return "unknown"

    @staticmethod
    def extract_token(headers: dict[str, str], query: dict[str, list[str]]) -> str | None:
        auth = headers.get("authorization")
        if auth:
            parts = auth.split()
            if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1]:
                return parts[1]
        values = query.get("token") or []
        for v in values:
            if v:
                return v
        return None

    async def _send_all(self, send, messages: list) -> None:
        for m in messages:
            await send(m)

    # -- request handling ----------------------------------------------------------

    async def _http(self, scope: dict, receive, send) -> None:
        path = scope.get("path") or "/"
        method = scope.get("method", "GET").upper()
        headers = self._headers(scope)
        if method == "GET" and path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
            await self._send_all(
                send, _json_response_messages(200, self._metadata(headers), {"Cache-Control": "public, max-age=300"})
            )
            return
        if method == "GET" and path == "/healthz":
            await self._send_all(send, _json_response_messages(200, {"status": "ok"}))
            return
        await self._mcp(scope, receive, send, headers, method)

    async def _mcp(self, scope: dict, receive, send, headers: dict[str, str], method: str) -> None:
        # 1. read the body
        chunks: list[bytes] = []
        size = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body_part = message.get("body", b"")
            size += len(body_part)
            if size > MAX_BODY:
                await self._send_all(send, _json_response_messages(413, rpc_error(-32600, "Request body too large.")))
                return
            chunks.append(body_part)
            if not message.get("more_body"):
                break
        body = b"".join(chunks)
        parsed: Any = None
        if body:
            try:
                parsed = json.loads(body)
            except ValueError:
                parsed = None
        # 2. batches
        if isinstance(parsed, list):
            await self._send_all(
                send,
                _json_response_messages(
                    400,
                    rpc_error(-32600, "Batch requests are not supported. Send one JSON-RPC message per POST."),
                ),
            )
            return
        rpc_method = parsed.get("method") if isinstance(parsed, dict) else None
        rpc_id = parsed.get("id") if isinstance(parsed, dict) else None

        sent = False

        async def replay():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        # 3. credential and session header
        query = parse_qs(scope.get("query_string", b"").decode("latin-1"), keep_blank_values=True)
        token = self.extract_token(headers, query)
        register_secret(token)
        if token and token.strip().lower() == PLACEHOLDER_TOKEN:
            api_token: str | None = None
        else:
            api_token = token
        fingerprint = token_fingerprint(token)
        payment_param = (query.get("payment") or [None])[0]
        session_id = headers.get("mcp-session-id")

        # 4. existing session
        entry: SessionEntry | None = None
        if session_id:
            candidate = self.sessions.get(session_id)
            if candidate is not None and hmac.compare_digest(candidate.token_hash, fingerprint):
                entry = candidate
            elif not token and not payment_param:
                await self._send_all(send, self._unauthorized(headers))
                return
            else:
                await self._send_all(send, _json_response_messages(404, rpc_error(-32600, "Session not found.")))
                return

        # 5. protocol task methods
        if rpc_method in TASK_METHODS:
            if entry is None and not token and (not payment_param or rpc_method == "tasks/list"):
                await self._send_all(send, self._unauthorized(headers))
                return
            if entry is not None:
                task_scope = entry.session_id
            elif token:
                task_scope = f"token:{fingerprint}"
            else:
                task_scope = "anonymous"
            params = parsed.get("params") if isinstance(parsed.get("params"), dict) else {}
            try:
                result = task_store.handle(task_scope, rpc_method, params)
                payload = {"jsonrpc": "2.0", "id": rpc_id, "result": result}
            except TaskError as exc:
                payload = rpc_error(exc.code, exc.message, rpc_id)
            await self._send_all(send, _json_response_messages(200, payload))
            return

        # 6. matched session
        if entry is not None:
            entry.inflight += 1
            entry.last_seen = time.monotonic()
            try:
                await entry.manager.handle_request(scope, replay, send)
            finally:
                entry.inflight -= 1
                entry.last_seen = time.monotonic()
            if method == "DELETE":
                self.close_session(entry.session_id)
            return

        # 7. payment provider
        payment_kind = parse_payment(payment_param)

        # 8. auth gate
        if payment_kind is None and not token and rpc_method != "server/discover":
            await self._send_all(send, self._unauthorized(headers))
            return

        ui_values = query.get("ui")
        ui_option = self.ui_option
        if ui_values:
            from .config import parse_ui_option

            ui_option = parse_ui_option(ui_values[0])
        telemetry = self.telemetry_enabled
        tel_values = query.get("telemetry-enabled")
        if tel_values:
            parsed_bool = parse_bool(tel_values[0])
            if parsed_bool is not None:
                telemetry = parsed_bool
        selection = self._url_selection(query)
        if selection is not None:
            n_actors = len(actor_selectors(selection))
            limit = self.settings.max_selected_actors
            if n_actors > limit:
                await self._send_all(
                    send,
                    _json_response_messages(
                        400,
                        rpc_error(
                            -32602,
                            f"Too many Actors selected ({n_actors}). Select at most {limit} Actors "
                            "with ?actors= or ?tools=.",
                            rpc_id,
                        ),
                    ),
                )
                return

        protocol_header = headers.get("mcp-protocol-version", "")
        # 9. stateless request
        if protocol_header.startswith("2026") or rpc_method == "server/discover":
            if not protocol_header:
                scope = dict(scope)
                scope["headers"] = list(scope.get("headers") or []) + [(b"mcp-protocol-version", b"2026-07-28")]
            payment = await make_provider(self.settings, payment_kind) if payment_kind else None
            try:
                state = await build_state(
                    self.settings,
                    token=api_token,
                    payment=payment,
                    ui_option=ui_option,
                    telemetry_enabled=telemetry,
                    transport="http",
                    selection=selection,
                    preloaded=self._preloaded,
                    task_scope=f"token:{fingerprint}" if token else "anonymous",
                    stateless=True,
                )
            except ToolCapacityError as exc:
                await self._send_all(send, self._at_capacity(exc, rpc_id))
                return
            try:
                server = ScrapelyServer(state)
                manager = StreamableHTTPSessionManager(app=server, stateless=True, session_idle_timeout=None)
                async with manager.run():
                    await manager.handle_request(scope, replay, send)
            finally:
                state.release_tools()
            return

        # 10. session creation needs initialize
        if rpc_method != "initialize":
            await self._send_all(send, _json_response_messages(400, rpc_error(-32600, "Missing session ID.", rpc_id)))
            return

        # 11. capacity
        keys = self._admission_keys(token, fingerprint, self.client_ip(scope, headers))
        refusal = await self._reserve(keys)
        if refusal == "key":
            log.soft_fail(
                "per-credential/per-IP session limit reached, refusing new session",
                max_per_credential=self.settings.max_sessions_per_credential,
                max_per_ip=self.settings.max_sessions_per_ip,
            )
            await self._send_all(
                send,
                _json_response_messages(
                    429,
                    rpc_error(
                        -32000,
                        "Too many open MCP sessions for this credential or client address. Close unused "
                        "sessions (HTTP DELETE with the Mcp-Session-Id header) or retry later.",
                    ),
                    {"Retry-After": "30"},
                ),
            )
            return
        if refusal is not None:
            log.soft_fail("session limit reached, refusing new session", max_sessions=self.settings.max_sessions)
            await self._send_all(
                send,
                _json_response_messages(
                    503,
                    rpc_error(-32000, "The server has too many open sessions. Retry in a few seconds."),
                    {"Retry-After": "5"},
                ),
            )
            return
        try:
            await self._open_session(
                scope, replay, send, api_token, fingerprint, payment_kind, ui_option, telemetry, selection, keys
            )
        except ToolCapacityError as exc:
            await self._send_all(send, self._at_capacity(exc, rpc_id))
        finally:
            self._release(keys)

    @staticmethod
    def _at_capacity(exc: ToolCapacityError, rpc_id: Any) -> list:
        log.soft_fail("process-wide Actor tool budget is full, refusing the request")
        return _json_response_messages(503, rpc_error(-32000, str(exc), rpc_id), {"Retry-After": "30"})

    def _url_selection(self, query: dict[str, list[str]]) -> Selection | None:
        if not any(k in query for k in ("tools", "actors", "maxActorMemoryBytes")):
            return None
        max_mem = None
        raw = (query.get("maxActorMemoryBytes") or [None])[0]
        if raw is not None:
            try:
                max_mem = int(raw)
            except ValueError:
                log.warning("ignoring non-integer maxActorMemoryBytes", value=raw[:50])
        actors = query.get("actors")
        tools = query.get("tools")
        return normalize_selection(actors if actors is not None else None, tools if tools is not None else None, max_mem)

    def _admission_keys(self, token: str | None, fingerprint: str, ip: str) -> tuple[str, ...]:
        keys = [f"ip:{ip}"]
        if token:
            keys.insert(0, f"cred:{fingerprint}")
        return tuple(keys)

    def _key_cap(self, key: str) -> int:
        if key.startswith("cred:"):
            return self.settings.max_sessions_per_credential
        return self.settings.max_sessions_per_ip

    def _key_count(self, key: str) -> int:
        return len(self._by_key.get(key, ())) + self._reserved_by_key.get(key, 0)

    def _lru_idle(self, ids) -> SessionEntry | None:
        idle = [e for e in (self.sessions.get(i) for i in ids) if e is not None and e.inflight == 0]
        return min(idle, key=lambda e: e.last_seen) if idle else None

    async def _reserve(self, keys: tuple[str, ...] = ()) -> str | None:
        """Reserve a slot for a new session. Returns None, "key" or "global".

        A caller over its own per-key cap only ever evicts its own idle
        sessions. When the whole pool is full, the caller's own idle sessions
        go first; another caller's idle session is evicted only as a last resort.
        """
        async with self._lock:
            for key in keys:
                if self._key_count(key) >= self._key_cap(key):
                    victim = self._lru_idle(list(self._by_key.get(key, ())))
                    if victim is None:
                        return "key"
                    self.close_session(victim.session_id)
            if len(self.sessions) + self.reserving >= self.settings.max_sessions:
                victim = None
                for key in keys:
                    victim = self._lru_idle(list(self._by_key.get(key, ())))
                    if victim is not None:
                        break
                if victim is None:
                    victim = self._lru_idle(list(self.sessions))
                if victim is None:
                    return "global"
                self.close_session(victim.session_id)
            self.reserving += 1
            for key in keys:
                self._reserved_by_key[key] = self._reserved_by_key.get(key, 0) + 1
            return None

    def _release(self, keys: tuple[str, ...]) -> None:
        self.reserving -= 1
        for key in keys:
            left = self._reserved_by_key.get(key, 0) - 1
            if left > 0:
                self._reserved_by_key[key] = left
            else:
                self._reserved_by_key.pop(key, None)

    async def _open_session(
        self, scope, replay, send, token, fingerprint, payment_kind, ui_option, telemetry, selection, keys=()
    ):
        payment = await make_provider(self.settings, payment_kind) if payment_kind else None
        state = await build_state(
            self.settings,
            token=token,
            payment=payment,
            ui_option=ui_option,
            telemetry_enabled=telemetry,
            transport="http",
            selection=selection,
            preloaded=self._preloaded,
        )
        server = ScrapelyServer(state)
        manager = StreamableHTTPSessionManager(app=server, session_idle_timeout=None, max_sessions=None)
        stop = anyio.Event()

        async def runner(*, task_status=anyio.TASK_STATUS_IGNORED) -> None:
            async with manager.run():
                task_status.started()
                await stop.wait()

        assert self._tg is not None, "gateway lifespan has not started"
        await self._tg.start(runner)
        registered: dict[str, SessionEntry] = {}

        async def watch(message: dict) -> None:
            if message["type"] == "http.response.start" and message.get("status", 500) < 400:
                for k, v in message.get("headers") or []:
                    if k.decode("latin-1").lower() == "mcp-session-id":
                        sid = v.decode("latin-1")
                        state.session_id = sid
                        state.task_scope = sid
                        # In flight until the initialize response is done, so a
                        # full pool cannot evict it as idle mid-handshake.
                        entry = SessionEntry(sid, fingerprint, server, manager, stop, inflight=1, keys=keys)
                        self._register(entry)
                        registered["entry"] = entry
            await send(message)

        try:
            await manager.handle_request(scope, replay, watch)
        finally:
            if "entry" not in registered:
                state.release_tools()
                stop.set()
            else:
                registered["entry"].inflight -= 1
                registered["entry"].last_seen = time.monotonic()


def create_app(**kwargs: Any) -> Gateway:
    return Gateway(**kwargs)


def fingerprint_hex(token: str | None) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()
