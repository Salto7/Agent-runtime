"""Parse MCP server documents. No database and no process starts.

Accepts one server object or an ``mcpServers`` map. A host's ``McpStore``
(see ``orchestrator.mcp.store``) should run this before every create or
replace — the parser is what rejects a bad document, the store is just
persistence.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

_SECRET_KEYS = frozenset(
    {
        "token",
        "password",
        "secret",
        "api_key",
        "apikey",
        "authorization",
        "access_token",
        "refresh_token",
    }
)
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ENV_TYPES = frozenset({"string", "secret", "boolean", "number", "choice"})
_ENV_FIELD_KEYS = frozenset({"name", "required", "type", "description", "default", "choices"})


def _looks_secret(name: str) -> bool:
    low = name.lower()
    return any(key in low for key in _SECRET_KEYS)


@dataclass(frozen=True)
class McpSpec:
    mcp_id: str
    description: str
    transport: str
    command: str
    args: tuple[str, ...]
    cwd: str
    install: dict[str, Any] | None
    env_schema: tuple[tuple[str, bool], ...]
    reference_hosts: tuple[str, ...] = ()
    # Full per-field metadata (type/description/default/choices); env_schema
    # above stays a plain (name, required) tuple so every existing call site
    # that destructures it keeps working untouched.
    env_schema_full: tuple[dict[str, Any], ...] = field(default_factory=tuple)


class McpParseError(ValueError):
    """The document is not a usable MCP server spec."""


def parse_mcp_document(document: dict[str, Any] | str | list[Any]) -> tuple[McpSpec, ...]:
    """Return one spec per server. Raises ``McpParseError`` on invalid input."""
    data = _load(document)
    _reject_secrets(data)
    if isinstance(data, dict) and isinstance(data.get("mcpServers"), dict):
        servers = data["mcpServers"]
        specs = []
        seen: set[str] = set()
        for name, body in servers.items():
            if not isinstance(body, dict):
                raise McpParseError(f"mcpServers.{name} must be an object")
            merged = dict(body)
            merged.setdefault("name", name)
            spec = _one(merged)
            if spec.mcp_id in seen:
                raise McpParseError(f"duplicate server {spec.mcp_id!r}")
            seen.add(spec.mcp_id)
            specs.append(spec)
        if not specs:
            raise McpParseError("mcpServers is empty")
        return tuple(specs)
    if isinstance(data, dict):
        return (_one(data),)
    raise McpParseError("MCP document must be an object or mcpServers map")


def _load(document: dict[str, Any] | str | list[Any]) -> Any:
    if isinstance(document, str):
        text = document.strip()
        if not text:
            raise McpParseError("empty MCP document")
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise McpParseError(f"MCP document is not JSON: {exc}") from exc
    return document


def _one(raw: dict[str, Any]) -> McpSpec:
    name = str(raw.get("name") or raw.get("mcp_id") or raw.get("id") or "").strip().lower()
    if not name or len(name) > 128 or not SKILL_NAME_RE.match(name):
        raise McpParseError(
            "server name is required and must be kebab-case "
            f"(got {name!r})"
        )
    transport = str(raw.get("transport") or raw.get("type") or "stdio").strip().lower()
    if transport in {"sse", "http", "streamable-http", "websocket"}:
        raise McpParseError(
            f"transport {transport!r} is not supported; the sandbox runs stdio servers"
        )
    if transport != "stdio":
        raise McpParseError("only transport stdio is supported")
    command, args = _command(raw)
    description = str(raw.get("description") or "").strip()
    if len(description) > 4000:
        raise McpParseError("description is too long")
    install = _install(raw.get("install"))
    env_fields = _env_schema(raw.get("env_schema") or raw.get("env"))
    return McpSpec(
        mcp_id=name,
        description=description,
        transport=transport,
        command=command,
        args=args,
        cwd=_cwd(raw.get("cwd")),
        install=install,
        env_schema=tuple((f["name"], bool(f["required"])) for f in env_fields),
        env_schema_full=tuple(env_fields),
        reference_hosts=_reference_hosts(raw.get("reference_hosts")),
    )


def _command(raw: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    command_raw = raw.get("command")
    args_raw = raw.get("args") if raw.get("args") is not None else []
    if not isinstance(args_raw, list) or not all(isinstance(item, str) for item in args_raw):
        raise McpParseError("args must be a list of strings")
    if isinstance(command_raw, list):
        if not command_raw or not all(isinstance(item, str) for item in command_raw):
            raise McpParseError("command list must be non-empty strings")
        command = command_raw[0].strip()
        args = tuple(item for item in (*command_raw[1:], *args_raw))
    elif isinstance(command_raw, str):
        command = command_raw.strip()
        args = tuple(args_raw)
    else:
        raise McpParseError("command is required")
    if not command or len(command) > 256 or any(ch in command for ch in "\n\r\x00"):
        raise McpParseError("command is required and must be a single line")
    # Sandbox PATH only — reject host absolute paths from Desktop configs.
    if command.startswith(("/", "~")) or "/" in command or "\\" in command:
        raise McpParseError(
            "command must be a bare executable on the sandbox PATH "
            "(python, python3, node, npm, npx, uv, uvx, …), not a host filesystem path"
        )
    if len(args) > 64:
        raise McpParseError("too many args")
    if any(any(ch in item for ch in "\n\r\x00") or len(item) > 4096 for item in args):
        raise McpParseError("args must be single-line strings")
    return command, args


def _cwd(raw: Any) -> str:
    if raw in (None, "", "."):
        return ""
    text = str(raw).strip().replace("\\", "/")
    if text.startswith(("/", "~")):
        raise McpParseError("cwd must be a relative path inside the server directory")
    parts = [part for part in text.split("/") if part not in {"", "."}]
    if not parts or any(part == ".." for part in parts):
        raise McpParseError("cwd must be a relative path inside the server directory")
    if len("/".join(parts)) > 256:
        raise McpParseError("cwd is too long")
    return "/".join(parts)


def _install(raw: Any) -> dict[str, Any] | None:
    if raw in (None, "", {}):
        return None
    if not isinstance(raw, dict):
        raise McpParseError("install must be an object")
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in {"pip", "uv", "npm", "none"}:
        raise McpParseError("install.kind must be pip, uv, npm, or none")
    if kind == "none":
        return None
    default_manifest = {
        "pip": "requirements.txt",
        "uv": "pyproject.toml",
        "npm": "package.json",
    }.get(kind, "requirements.txt")
    manifest = str(raw.get("manifest") or "").strip() or default_manifest
    if ".." in manifest.split("/") or manifest.startswith("/"):
        raise McpParseError("install.manifest must be a relative file name")
    spec: dict[str, Any] = {"kind": kind, "manifest": manifest}
    post = _post_install(raw.get("post"))
    if post:
        spec["post"] = post
    return spec


def _post_install(raw: Any) -> list[str]:
    """Optional one-off setup commands run in the MCP's environment after install.

    For assets a package manager cannot fetch itself (e.g. a browser binary,
    model weights). Each entry is a single-line shell command.
    """
    if raw in (None, "", []):
        return []
    if not isinstance(raw, list) or not all(isinstance(c, str) for c in raw):
        raise McpParseError("install.post must be a list of command strings")
    cmds = [c.strip() for c in raw if c.strip()]
    if len(cmds) > 8 or any(len(c) > 500 or "\n" in c or "\x00" in c for c in cmds):
        raise McpParseError("install.post: up to 8 single-line commands (<=500 chars)")
    return cmds


def _reference_hosts(raw: Any) -> tuple[str, ...]:
    """Hostnames the server queries as reference infrastructure (not targets)."""
    if raw in (None, "", []):
        return ()
    if not isinstance(raw, list) or not all(isinstance(h, str) for h in raw):
        raise McpParseError("reference_hosts must be a list of hostnames")
    hosts = []
    for h in raw:
        host = h.strip().lower().rstrip(".")
        if not host or "/" in host or " " in host or len(host) > 253:
            raise McpParseError(f"invalid reference host {h!r}")
        hosts.append(host)
    return tuple(dict.fromkeys(hosts))


def _env_schema(raw: Any) -> list[dict[str, Any]]:
    """Per-field env metadata: name/required, plus optional type/description/default/choices.

    Values themselves are never stored in the catalog — only the declaration.
    ``type`` defaults to "secret" for names that look like credentials, else
    "string"; "boolean"/"number"/"choice" let the per-project settings UI
    render a checkbox/number input/select instead of a bare text box.
    """
    if raw in (None, "", []):
        return []
    if isinstance(raw, dict):
        filled = [
            str(key)
            for key, value in raw.items()
            if value is not None and str(value).strip()
        ]
        if filled:
            raise McpParseError(
                "env values are not stored in the catalog "
                f"({', '.join(filled)}). Declare the names and set values on the project."
            )
        raw = list(raw.keys())
    if not isinstance(raw, list):
        raise McpParseError("env_schema must be a list")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            entry: dict[str, Any] = {"name": item.strip(), "required": True}
        elif isinstance(item, dict):
            entry = dict(item)
        else:
            raise McpParseError("env_schema entries must be strings or objects")
        extra = set(entry) - _ENV_FIELD_KEYS
        if extra:
            raise McpParseError(f"env_schema entries only allow {', '.join(sorted(_ENV_FIELD_KEYS))}")
        name = str(entry.get("name") or "").strip()
        if not _ENV_NAME.match(name):
            raise McpParseError(f"invalid env name {name!r}")
        if name in seen:
            raise McpParseError(f"duplicate env name {name!r}")
        seen.add(name)
        required = bool(entry.get("required", True))
        etype = str(entry.get("type") or "").strip().lower()
        if not etype:
            etype = "secret" if _looks_secret(name) else "string"
        if etype not in _ENV_TYPES:
            raise McpParseError(
                f"env_schema[{name}].type must be one of {', '.join(sorted(_ENV_TYPES))}"
            )
        description = str(entry.get("description") or "").strip()
        if len(description) > 500:
            raise McpParseError(f"env_schema[{name}].description is too long")
        default = entry.get("default")
        default = "" if default is None else str(default)
        choices_raw = entry.get("choices") or []
        if etype == "choice":
            if not isinstance(choices_raw, list) or not choices_raw:
                raise McpParseError(f"env_schema[{name}]: type=choice needs a non-empty choices list")
            choices = [str(c) for c in choices_raw]
        elif choices_raw:
            raise McpParseError(f"env_schema[{name}]: choices only applies to type=choice")
        else:
            choices = []
        out.append(
            {
                "name": name,
                "required": required,
                "type": etype,
                "description": description,
                "default": default,
                "choices": choices,
            }
        )
    return out


def _reject_secrets(value: Any, path: str = "") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            key_s = str(key).strip().lower().replace("-", "_")
            here = f"{path}{key}."
            if key_s in _SECRET_KEYS and isinstance(item, str) and item.strip():
                raise McpParseError(f"do not put secret values in the catalog ({path}{key})")
            _reject_secrets(item, here)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secrets(item, f"{path}{index}.")
