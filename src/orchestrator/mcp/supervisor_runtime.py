#!/usr/bin/env python3
"""In-sandbox MCP supervisor (official mcp SDK client).

Shipped with the library and baked into the sandbox image at
``/opt/orchestrator-mcp/supervisor.py``. The worker launches this file with the
image MCP client venv; it is a normal Python module (not an embedded string).

Protocol: one NDJSON request per Unix-socket connection, one NDJSON reply.
Each connection is served on its own thread, so a long tool call never blocks
other agents. Each MCP server is owned by one long-lived asyncio task that
enters and exits the SDK contexts itself (anyio cancel scopes must be closed
by the task that opened them).
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import os
import socket
import threading
import uuid
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Fixed sandbox paths (not env-driven — avoids host-controlled path injection).
SOCK = "/tmp/orchestrator-mcp/supervisor.sock"
PID = "/tmp/orchestrator-mcp/supervisor.pid"
LOG_DIR = Path("/tmp/orchestrator-mcp")
ARTIFACT_ROOT = Path("/workspace/workspace/mcp/artifacts")
_MAX_REQUEST = 4_000_000
_RESERVED_ENV = frozenset(
    {
        "PATH",
        "HOME",
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "PYTHONPATH",
        "NODE_OPTIONS",
        "PYTHONHOME",
    }
)


def _bounded_int(name: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(int(os.environ.get(name, str(default))), hi))
    except ValueError:
        return default


# Spill when the full JSON dump exceeds this many characters.
SPILL_CHARS = _bounded_int("MCP_SPILL_CHARS", 6000, 1000, 200_000)
PREVIEW_CHARS = _bounded_int("MCP_PREVIEW_CHARS", 4000, 500, 50_000)
STARTUP_SECONDS = _bounded_int("MCP_STARTUP_SECONDS", 600, 30, 1800)


def _safe_token(text: str, fallback: str = "server") -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in text)
    return cleaned or fallback


def _fingerprint(spec: dict) -> str:
    # ``revision`` changes when the server files or install change, so an
    # edited server is restarted instead of serving stale code.
    return json.dumps(
        {
            "command": spec.get("command") or "",
            "args": spec.get("args") or [],
            "cwd": spec.get("cwd") or "",
            "env": spec.get("env") or {},
            "revision": spec.get("revision") or "",
        },
        sort_keys=True,
        default=str,
    )


def _timeout(spec: dict, default: float) -> float:
    try:
        value = float(spec.get("timeout") or default)
    except (TypeError, ValueError):
        value = default
    return max(5.0, min(value, 600.0))


def _dump(obj: Any) -> Any:
    if obj is None:
        return None
    if hasattr(obj, "model_dump"):
        return obj.model_dump(by_alias=True, exclude_none=True, mode="json")
    if hasattr(obj, "dict"):
        return obj.dict()
    return obj


def _tool_dict(tool: Any) -> dict[str, Any]:
    data = _dump(tool)
    if not isinstance(data, dict):
        return {"name": str(getattr(tool, "name", "") or ""), "description": ""}
    out: dict[str, Any] = {
        "name": str(data.get("name") or ""),
        "description": str(data.get("description") or ""),
    }
    schema = data.get("inputSchema")
    if isinstance(schema, dict):
        out["inputSchema"] = schema
    return out


def _resource_dict(resource: Any) -> dict[str, Any]:
    data = _dump(resource)
    if not isinstance(data, dict):
        return {"uri": str(getattr(resource, "uri", "") or "")}
    out: dict[str, Any] = {
        "uri": str(data.get("uri") or ""),
        "name": str(data.get("name") or ""),
    }
    if data.get("description"):
        out["description"] = str(data.get("description"))
    if data.get("mimeType"):
        out["mimeType"] = str(data.get("mimeType"))
    return out


def _child_env(spec: dict) -> dict[str, str] | None:
    extra = spec.get("env") or {}
    if not isinstance(extra, dict) or not extra:
        return None
    env = {
        name: str(value)
        for key, value in extra.items()
        if (name := str(key or "").strip()) and name not in _RESERVED_ENV
    }
    return env or None


def _text_preview(payload: Any, limit: int) -> str:
    if isinstance(payload, dict) and isinstance(payload.get("content"), list):
        parts: list[str] = []
        for block in payload["content"]:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            else:
                parts.append(json.dumps(block, default=str))
        text = "\n".join(parts)
    else:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "\n…(preview; full payload in artifact)"


def _resource_uris(payload: Any) -> list[str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("content"), list):
        return []
    uris: list[str] = []
    for block in payload["content"]:
        if not isinstance(block, dict):
            continue
        # EmbeddedResource / ResourceLink shapes from the MCP SDK dump.
        resource = block.get("resource") if block.get("type") == "resource" else None
        if isinstance(resource, dict) and resource.get("uri"):
            uris.append(str(resource["uri"]))
        if block.get("uri") and block.get("type") in {"resource_link", "resource"}:
            uris.append(str(block["uri"]))
    return list(dict.fromkeys(u for u in uris if u))


def _structured_body(payload: Any) -> str | None:
    """The JSON document a tool result carries, or None when it is not one.

    MCP wraps results as ``{"content": [{"type": "text", "text": "<json>"}]}``.
    Persisting that envelope forces every consumer (agents, report builders,
    ``jq``) to unwrap a JSON string inside JSON. When the result is a single
    successful text block that is itself a JSON document, the artifact stores
    the document directly. Anything else keeps the full envelope.
    """
    if not isinstance(payload, dict) or payload.get("isError"):
        return None
    content = payload.get("content")
    if not isinstance(content, list) or len(content) != 1:
        return None
    block = content[0]
    if not isinstance(block, dict) or block.get("type") != "text":
        return None
    text = block.get("text")
    if not isinstance(text, str) or not text.lstrip().startswith(("{", "[")):
        return None
    try:
        json.loads(text)
    except ValueError:
        return None
    return text


def _maybe_spill(mcp_id: str, kind: str, name: str, payload: Any) -> Any:
    """Persist large payloads; return envelope with workspace-relative path."""
    raw = json.dumps(payload, ensure_ascii=False, default=str)
    if len(raw) <= SPILL_CHARS:
        return payload
    structured = _structured_body(payload)
    dest_dir = ARTIFACT_ROOT / _safe_token(mcp_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    path = dest_dir / f"{_safe_token(name, kind)}-{uuid.uuid4().hex[:12]}.json"
    body = structured if structured is not None else raw
    path.write_text(body, encoding="utf-8")
    try:
        rel = str(path.relative_to("/workspace"))
    except ValueError:
        rel = str(path)
    return {
        "ok": True,
        "mcp_id": mcp_id,
        "kind": kind,
        "name": name,
        "artifact": rel,
        "format": "json" if structured is not None else "mcp-result",
        "bytes": len(body.encode("utf-8")),
        "preview": _text_preview(payload, PREVIEW_CHARS),
        "resources": _resource_uris(payload),
    }


class Server:
    """One running MCP server, owned by a single asyncio task."""

    def __init__(self, mcp_id: str, spec: dict) -> None:
        self.mcp_id = mcp_id
        self.fp = _fingerprint(spec)
        self.spec = spec
        self.session: ClientSession | None = None
        self.ready = asyncio.Event()
        self.stop = asyncio.Event()
        self.error: BaseException | None = None
        self.task = asyncio.get_running_loop().create_task(self._own())

    async def _own(self) -> None:
        spec = self.spec
        cwd = str(spec.get("cwd") or "/workspace")
        Path(cwd).mkdir(parents=True, exist_ok=True)
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        params = StdioServerParameters(
            command=str(spec.get("command") or ""),
            args=[str(a) for a in (spec.get("args") or [])],
            cwd=cwd,
            env=_child_env(spec),
        )
        try:
            with open(LOG_DIR / f"{_safe_token(self.mcp_id)}.log", "a", encoding="utf-8") as errlog:
                async with AsyncExitStack() as stack:
                    read, write = await stack.enter_async_context(
                        stdio_client(params, errlog=errlog)
                    )
                    session = await stack.enter_async_context(ClientSession(read, write))
                    # First start may include ``uv run`` syncing deps or ``npx``
                    # downloading the package, so allow much longer than a call.
                    await asyncio.wait_for(session.initialize(), timeout=STARTUP_SECONDS)
                    self.session = session
                    self.ready.set()
                    await self.stop.wait()
        except BaseException as exc:  # noqa: BLE001 — surfaced to waiters
            self.error = exc
        finally:
            self.session = None
            self.ready.set()

    @property
    def alive(self) -> bool:
        return self.session is not None and not self.task.done()

    async def shutdown(self) -> None:
        self.stop.set()
        try:
            await asyncio.wait_for(asyncio.shield(self.task), timeout=10)
        except (asyncio.TimeoutError, Exception):
            self.task.cancel()


_DISCONNECT_TYPES = frozenset({"ClosedResourceError", "BrokenResourceError", "EndOfStream"})


def _is_disconnect(exc: BaseException) -> bool:
    """True when the stdio pipe to the server is gone (not a tool-level error)."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if type(exc).__name__ in _DISCONNECT_TYPES or "connection closed" in str(exc).lower():
            return True
        inner = getattr(exc, "exceptions", None)  # ExceptionGroup
        if inner and any(_is_disconnect(e) for e in inner):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def _server_log_tail(mcp_id: str, lines: int = 15) -> str:
    try:
        text = (LOG_DIR / f"{_safe_token(mcp_id)}.log").read_text(errors="replace")
    except OSError:
        return ""
    return "\n".join(text.strip().splitlines()[-lines:])


class Hub:
    def __init__(self) -> None:
        self._servers: dict[str, Server] = {}
        self._lock = asyncio.Lock()

    async def close(self, mcp_id: str) -> None:
        async with self._lock:
            server = self._servers.pop(mcp_id, None)
        if server is not None:
            await server.shutdown()

    async def session_for(self, spec: dict) -> ClientSession:
        mcp_id = str(spec.get("mcp_id") or "").strip()
        if not mcp_id:
            raise ValueError("mcp_id is required")
        if not str(spec.get("command") or "").strip():
            raise ValueError("command is required")
        cwd = str(spec.get("cwd") or "/workspace")
        if not (cwd == "/workspace" or cwd.startswith("/workspace/")):
            raise ValueError("cwd must stay inside the sandbox workspace")
        fp = _fingerprint(spec)
        async with self._lock:
            server = self._servers.get(mcp_id)
            if server is not None and (server.fp != fp or server.task.done()):
                self._servers.pop(mcp_id, None)
                await server.shutdown()
                server = None
            if server is None:
                server = Server(mcp_id, spec)
                self._servers[mcp_id] = server
        await server.ready.wait()
        if server.session is None:
            async with self._lock:
                if self._servers.get(mcp_id) is server:
                    self._servers.pop(mcp_id, None)
            detail = f"{type(server.error).__name__}: {server.error}" if server.error else "exited"
            tail = _server_log_tail(mcp_id)
            raise RuntimeError(
                f"MCP server {mcp_id!r} failed to start ({detail})"
                + (f"\n--- server stderr ---\n{tail}" if tail else "")
            )
        return server.session

    async def run(self, spec: dict, call) -> Any:
        """Run ``call(session)``; restart the server once if its pipe died."""
        mcp_id = str(spec.get("mcp_id") or "")
        try:
            return await call(await self.session_for(spec))
        except Exception as exc:
            if not _is_disconnect(exc):
                raise
        # Child exited (crash, OOM, killed): start a fresh one and retry once.
        await self.close(mcp_id)
        return await call(await self.session_for(spec))

    async def list_tools(self, spec: dict) -> list[dict[str, Any]]:
        result = await self.run(
            spec, lambda s: asyncio.wait_for(s.list_tools(), timeout=_timeout(spec, 60))
        )
        return [_tool_dict(tool) for tool in (getattr(result, "tools", None) or [])]

    async def call_tool(self, spec: dict, tool: str, arguments: dict) -> Any:
        result = await self.run(
            spec,
            lambda s: asyncio.wait_for(
                s.call_tool(tool, arguments=arguments), timeout=_timeout(spec, 90)
            ),
        )
        return _maybe_spill(str(spec.get("mcp_id") or ""), "tool", tool, _dump(result))

    async def list_resources(self, spec: dict) -> list[dict[str, Any]]:
        result = await self.run(
            spec, lambda s: asyncio.wait_for(s.list_resources(), timeout=_timeout(spec, 60))
        )
        return [_resource_dict(item) for item in (getattr(result, "resources", None) or [])]

    async def read_resource(self, spec: dict, uri: str) -> Any:
        result = await self.run(
            spec, lambda s: asyncio.wait_for(s.read_resource(uri), timeout=_timeout(spec, 90))
        )
        return _maybe_spill(str(spec.get("mcp_id") or ""), "resource", uri, _dump(result))


HUB = Hub()
LOOP = asyncio.new_event_loop()


def _loop_thread() -> None:
    asyncio.set_event_loop(LOOP)
    LOOP.run_forever()


def _run(coro, timeout: float):
    future = asyncio.run_coroutine_threadsafe(coro, LOOP)
    try:
        # The call has its own ``wait_for``; a cold start may add STARTUP_SECONDS.
        return future.result(timeout=timeout + STARTUP_SECONDS + 5)
    except concurrent.futures.TimeoutError:
        future.cancel()
        raise TimeoutError(f"MCP request timed out after {int(timeout)}s") from None


def _handle(req: dict) -> dict:
    op = str(req.get("op") or "")
    if op == "list_tools":
        return {"ok": True, "tools": _run(HUB.list_tools(req), _timeout(req, 60))}
    if op == "call":
        tool = str(req.get("tool") or "").strip()
        if not tool:
            raise ValueError("tool is required")
        arguments = req.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")
        return {"ok": True, "result": _run(HUB.call_tool(req, tool, arguments), _timeout(req, 90))}
    if op == "list_resources":
        return {"ok": True, "resources": _run(HUB.list_resources(req), _timeout(req, 60))}
    if op == "read_resource":
        uri = str(req.get("uri") or "").strip()
        if not uri:
            raise ValueError("uri is required")
        return {"ok": True, "result": _run(HUB.read_resource(req, uri), _timeout(req, 90))}
    if op == "close":
        _run(HUB.close(str(req.get("mcp_id") or "")), 30)
        return {"ok": True}
    if op == "ping":
        return {"ok": True}
    raise ValueError(f"unknown op {op!r}")


def _serve_conn(conn: socket.socket) -> None:
    try:
        conn.settimeout(30)
        buf = b""
        while b"\n" not in buf and len(buf) <= _MAX_REQUEST:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf += chunk
        conn.settimeout(None)
        line = buf.split(b"\n", 1)[0]
        try:
            req = json.loads(line.decode("utf-8")) if line else {}
            if not isinstance(req, dict):
                raise ValueError("request must be an object")
            reply = _handle(req)
        except Exception as exc:  # noqa: BLE001 — every failure becomes a reply
            reply = {"ok": False, "error": str(exc) or type(exc).__name__}
        conn.sendall((json.dumps(reply, default=str) + "\n").encode("utf-8"))
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def serve() -> None:
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=_loop_thread, name="mcp-hub", daemon=True).start()
    path = Path(SOCK)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    Path(PID).write_text(str(os.getpid()), encoding="utf-8")
    server.listen(64)
    while True:
        conn, _ = server.accept()
        threading.Thread(target=_serve_conn, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    serve()
