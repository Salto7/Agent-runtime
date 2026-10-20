"""Structured stream envelopes: JSON metadata with precise getters.

``StreamMessage.content`` stays human-readable for the live feed.
Machine consumers read ``metadata.event`` + ``metadata.payload`` via the
helpers below — never by regex / prefix-parsing of ``content``.

Envelope shape::

    {
      "event": "run_cli",
      "payload": {"command": "nmap …", "cli": "nmap", "hosts": ["10.0.0.1"]},
      # optional routing keys: role, tag, project_id, job_id, …
    }
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from orchestrator.utils.commands import catalog_cli_in_command

# Stable event ids (keep short, snake_case).
EVENT_RUN_CLI = "run_cli"
EVENT_RUN_PERIODIC = "run_periodic"
EVENT_PROVISION_CLI = "provision_cli"
EVENT_SPAWN_WORK = "spawn_work"
EVENT_WORK_FINISHED = "work_finished"
EVENT_LIST_MCP_TOOLS = "list_mcp_tools"
EVENT_CALL_MCP_TOOL = "call_mcp_tool"
EVENT_LIST_MCP_RESOURCES = "list_mcp_resources"
EVENT_READ_MCP_RESOURCE = "read_mcp_resource"
EVENT_REPORT_NOTE = "report_note"
EVENT_READ_ARTIFACT = "read_artifact"
EVENT_WRITE_ARTIFACT = "write_artifact"
EVENT_RECORD_FINDING = "record_finding"

_HOST_TOKEN_RE = re.compile(
    r"(?:https?://[^\s\"']+|"
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b|"
    r"\b[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9-]{1,63})+\b)",
    flags=re.I,
)


def envelope(
    event: str,
    payload: Mapping[str, Any] | None = None,
    **meta: Any,
) -> dict[str, Any]:
    """Build a metadata dict with ``event`` + ``payload`` (plus optional keys)."""
    out: dict[str, Any] = dict(meta)
    name = str(event or "").strip()
    if name:
        out["event"] = name
    out["payload"] = dict(payload or {})
    return out


def event_of(meta: Mapping[str, Any] | None) -> str:
    if not isinstance(meta, Mapping):
        return ""
    return str(meta.get("event") or "").strip()


def payload_of(meta: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(meta, Mapping):
        return {}
    raw = meta.get("payload")
    return dict(raw) if isinstance(raw, Mapping) else {}


def payload_get(meta: Mapping[str, Any] | None, key: str, default: Any = None) -> Any:
    return payload_of(meta).get(key, default)


def payload_str(meta: Mapping[str, Any] | None, key: str) -> str:
    val = payload_get(meta, key, "")
    return str(val or "").strip()


def payload_command(meta: Mapping[str, Any] | None) -> str:
    return payload_str(meta, "command")


def payload_cli(meta: Mapping[str, Any] | None) -> str:
    return payload_str(meta, "cli").lower()


def payload_hosts(meta: Mapping[str, Any] | None) -> list[str]:
    raw = payload_get(meta, "hosts", None)
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        host = str(item or "").strip().lower()
        if not host or host in seen:
            continue
        seen.add(host)
        out.append(host)
    return out


def hosts_in_command(text: str) -> list[str]:
    """Network targets in a shell command (IPs / hostnames / URL hosts).

    Path-like tokens (``workspace/10.0.0.1.xml``) are ignored — an IP in a
    filename is not a probe target. Bare tokens and ``http(s)://`` URLs count.
    """
    out: list[str] = []
    seen: set[str] = set()
    for raw in (text or "").split():
        token = raw.strip().strip("'\"")
        if not token:
            continue
        lower = token.lower()
        is_url = lower.startswith(("http://", "https://"))
        # Filesystem paths are local args, not network targets.
        if not is_url and ("/" in token or "\\" in token):
            continue
        for match in _HOST_TOKEN_RE.findall(token):
            host = _normalize_host_token(match)
            if not host or host in seen:
                continue
            seen.add(host)
            out.append(host)
    return out


def cli_payload(command: str, **extra: Any) -> dict[str, Any]:
    """Payload for shell-runner events (``run_cli`` / ``run_periodic``)."""
    cmd = (command or "").strip()
    payload: dict[str, Any] = {
        "command": cmd,
        "cli": catalog_cli_in_command(cmd),
        "hosts": hosts_in_command(cmd),
    }
    payload.update(extra)
    return payload



def is_event(meta: Mapping[str, Any] | None, *names: str) -> bool:
    current = event_of(meta).lower()
    return bool(current) and current in {str(n).strip().lower() for n in names if n}


def _normalize_host_token(token: str) -> str:
    raw = (token or "").strip().rstrip(".,;)]}")
    if not raw:
        return ""
    lower = raw.lower()
    if lower.startswith(("http://", "https://")):
        try:
            host = (urlparse(raw).hostname or "").strip().lower()
        except Exception:
            return ""
        return host
    try:
        return str(ipaddress.ip_address(raw.split("%", 1)[0])).lower()
    except ValueError:
        pass
    return lower
