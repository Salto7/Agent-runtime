"""Install MCP servers from disk, git, or npx into a host-provided ``McpStore``.

No network calls beyond ``git clone``/``npx`` themselves; nothing here starts
the installed server. Callers that want persistence to disk (so a restart
doesn't lose imported packs) pass ``mcps_dir``; without it, imports still work
but only live in the ``McpStore``.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from orchestrator.mcp.parse import McpParseError, McpSpec, SKILL_NAME_RE, parse_mcp_document
from orchestrator.mcp.store import InMemoryMcpStore, McpRecord, McpStore
from orchestrator.utils.git_source import git_clone, normalize_git_source
from orchestrator.utils.install_skip import (
    copytree_ignore,
    public_manifest_name,
    skip_dir_name,
    skip_rel_path,
)
from orchestrator.utils.paths import safe_join
from orchestrator.utils.service import SharedService

_PKG = re.compile(r"^(@[A-Za-z0-9._-]+/)?[A-Za-z0-9._-]+$")
# Standard / host-agnostic MCP manifests only.
# Hidden ``.mcp.json`` may be read for discovery; installs persist as ``mcp.json``.
_MANIFEST_NAMES = (
    "mcp.json",
    ".mcp.json",
    "server.json",
    "mcpServers.json",
)
_PYTHON_MCP_ENTRIES = (
    "mcp_server.py",
    "server.py",
    "main.py",
)
# Clone/upload directory names that say nothing about the real package —
# a git clone always lands at ``.../repo`` regardless of what's inside.
_GENERIC_PACK_DIR_NAMES = frozenset({"repo", "pack", "upload"})


class McpInstaller(SharedService):
    """Parses and persists MCP server packs into a ``McpStore``.

    ``store`` defaults to an in-memory reference store; pass a real one (DB-
    backed, file-backed, …) to make installs durable across restarts.
    ``mcps_dir``, if given, also copies each installed pack's files to
    ``mcps_dir/<mcp_id>/`` so ``refresh_root()`` can re-import after a restart
    even if the store itself was wiped.
    """

    def __init__(self, *, store: McpStore | None = None, mcps_dir: Path | None = None) -> None:
        self._store = store or InMemoryMcpStore()
        self._mcps_dir = Path(mcps_dir).resolve() if mcps_dir else None

    def install_source(self, kind: str, value: str, *, revision: str = "") -> list[str]:
        """Create catalog MCP rows. ``kind`` is disk | git | npx."""
        mode = str(kind or "").strip().lower()
        raw = str(value or "").strip()
        if not raw:
            raise ValueError("source value is required")
        if mode == "disk":
            path = Path(raw).expanduser()
            if path.is_file():
                return self._from_disk(path.resolve(), persist=True)
            return self._from_disk(_safe_dir(raw), persist=True)
        if mode == "git":
            return self._from_git(raw, revision=revision)
        if mode == "npx":
            return [self._from_npx(raw)]
        raise ValueError("kind must be disk, git, or npx")

    def install_relative_files(
        self,
        files: list[tuple[str, bytes]],
        *,
        origin: str = "drop",
    ) -> list[str]:
        """Rebuild a dropped folder from ``(relative path, bytes)`` pairs.

        Hidden files are skipped (``.env``, …). ``.mcp.json`` is accepted only as a
        source manifest and stored as ``mcp.json``. Browsers often omit ``.mcp.json``;
        a pack with ``mcp_server.py`` is still inferred as stdio.
        """
        if not files:
            raise ValueError("no files")
        with tempfile.TemporaryDirectory(prefix="mcp-drop-") as tmp:
            root = Path(tmp)
            kept = 0
            for rel, payload in files:
                mapped = _mcp_upload_rel(rel)
                if mapped is None:
                    continue
                target = safe_join(root, mapped)
                if target is None:
                    raise ValueError(f"unsafe path {rel!r}")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
                kept += 1
            if not kept:
                raise ValueError(
                    "upload contained only ignored / hidden paths "
                    "(.env, .venv, …). Browsers hide ``.mcp.json`` — add ``mcp.json`` "
                    "or include ``mcp_server.py``."
                )
            return self._from_disk(_unwrap_upload_root(root), origin=origin, persist=True)

    def refresh_root(self, root: Path) -> list[str]:
        """Re-import every MCP pack under ``root`` (e.g. ``mcps_dir``)."""
        base = Path(root)
        if not base.is_dir():
            return []
        ids: list[str] = []
        try:
            children = sorted(base.iterdir())
        except OSError as exc:
            raise ValueError(f"could not list {base}: {exc}") from exc
        for child in children:
            if not child.is_dir() or child.name.startswith("."):
                continue
            try:
                ids.extend(
                    self._from_disk(child, origin=f"refresh:{child.name}", persist=False)
                )
            except ValueError:
                continue
        return ids

    # -- persistence -----------------------------------------------------

    def _persist_pack(self, source: Path, mcp_id: str) -> Path | None:
        """Copy a pack into ``mcps_dir/<id>/``. Hidden paths are omitted. No-op without ``mcps_dir``."""
        if self._mcps_dir is None:
            return None
        self._mcps_dir.mkdir(parents=True, exist_ok=True)
        dest = self._mcps_dir / mcp_id
        if dest.exists():
            shutil.rmtree(dest)
        if source.is_file():
            dest.mkdir(parents=True, exist_ok=True)
            # Store discovery manifests under a non-hidden name for refresh.
            shutil.copy2(source, dest / public_manifest_name(source.name))
            return dest
        shutil.copytree(source, dest, ignore=copytree_ignore(skip_hidden=True), symlinks=False)
        return dest

    def _write_public_manifest(self, source_root: Path, mcp_id: str) -> None:
        """Ensure ``mcps_dir/<id>/mcp.json`` exists after an inferred install (refresh-safe)."""
        if self._mcps_dir is None:
            return
        dest = self._mcps_dir / mcp_id
        if not dest.is_dir():
            return
        public = dest / "mcp.json"
        if public.is_file():
            return
        # Prefer a host ``.mcp.json`` if the install came from a real disk path.
        for name in (".mcp.json", "mcp.json"):
            candidate = Path(source_root) / name
            if candidate.is_file():
                try:
                    public.write_text(candidate.read_text(encoding="utf-8"), encoding="utf-8")
                except OSError:
                    return
                return
        # Synthetic manifest matching the inferred stdio shape.
        entry = next((n for n in _PYTHON_MCP_ENTRIES if (dest / n).is_file()), "mcp_server.py")
        payload = {
            "name": mcp_id,
            "description": f"stdio MCP ({entry})",
            "transport": "stdio",
            "command": "python",
            "args": [entry],
            "env_schema": [],
        }
        if (dest / "pyproject.toml").is_file():
            payload["install"] = {"kind": "uv", "manifest": "pyproject.toml"}
        elif (dest / "requirements.txt").is_file():
            payload["install"] = {"kind": "pip", "manifest": "requirements.txt"}
        try:
            public.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        except OSError:
            return

    # -- source handlers ---------------------------------------------------

    def _from_npx(self, package: str) -> str:
        pkg = str(package or "").strip()
        if not pkg or any(ch in pkg for ch in "\n\r\x00 ") or not _PKG.match(pkg):
            raise ValueError("npx package must look like name or @scope/name (no spaces)")
        mcp_id = _slug_from_package(pkg)
        document = {
            "name": mcp_id,
            "description": f"MCP server installed from npx package {pkg}",
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", pkg],
            "env_schema": [],
        }
        return self._upsert(document, files=None)

    def _from_git(self, source: str, *, revision: str = "") -> list[str]:
        origin = normalize_git_source(source)
        with tempfile.TemporaryDirectory(prefix="mcp-git-") as tmp:
            dest = Path(tmp) / "repo"
            git_clone(origin, dest, revision=revision)
            return self._from_disk(dest, origin=f"git:{origin}", persist=True)

    def _from_disk(self, path: Path, *, origin: str = "", persist: bool = False) -> list[str]:
        target = Path(path).expanduser().resolve()
        if target.is_file():
            ids = self._upsert_file(target, origin=origin or f"disk:{target}")
            if persist and ids:
                self._persist_pack(target, ids[0])
            return ids
        if not target.is_dir():
            raise ValueError(f"not a file or directory: {target}")
        manifests = _find_manifests(target)
        if manifests:
            ids: list[str] = []
            for item in manifests:
                pack_ids = self._upsert_file(item, origin=origin or f"disk:{target}")
                ids.extend(pack_ids)
                if persist and pack_ids:
                    # Persist the pack directory that owns the manifest.
                    self._persist_pack(item.parent, pack_ids[0])
            return ids
        package = target / "package.json"
        if package.is_file():
            mid = self._from_npm_package(target, origin=origin or f"disk:{target}")
            if persist:
                self._persist_pack(target, mid)
            return [mid]
        inferred = self._from_python_stdio_pack(target, origin=origin or f"disk:{target}")
        if inferred:
            if persist:
                self._persist_pack(target, inferred)
                self._write_public_manifest(target, inferred)
            return [inferred]
        raise ValueError(
            "directory has no mcp.json / package.json to import. "
            "Browsers do not upload hidden files — a host-only ``.mcp.json`` is invisible "
            "to the folder picker; add a non-hidden ``mcp.json`` or keep ``mcp_server.py`` "
            "so it can be inferred as a stdio server."
        )

    def _upsert_file(self, path: Path, *, origin: str = "") -> list[str]:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ValueError(f"could not read {path}: {exc}") from exc
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path.name} is not JSON: {exc}") from exc
        sibling = (_collect_text_tree(path.parent) or None) if path.parent.is_dir() else None
        if isinstance(data, dict) and "mcpServers" in data:
            ids = []
            for name, body in (data.get("mcpServers") or {}).items():
                if not isinstance(body, dict):
                    continue
                merged = dict(body)
                merged.setdefault("name", name)
                if origin and not merged.get("description"):
                    merged["description"] = f"Imported from {origin}"
                ids.append(self._upsert(merged, files=sibling))
            if not ids:
                raise ValueError("mcpServers map is empty")
            return ids
        if isinstance(data, dict):
            if origin and not data.get("description"):
                data = {**data, "description": f"Imported from {origin}"}
            return [self._upsert(data, files=sibling)]
        raise ValueError(f"{path.name} must be an MCP server object or mcpServers map")

    def _from_python_stdio_pack(self, root: Path, *, origin: str = "") -> str | None:
        """Infer a stdio MCP when the folder picker omitted hidden ``.mcp.json``."""
        entry = ""
        for name in _PYTHON_MCP_ENTRIES:
            if (root / name).is_file():
                entry = name
                break
        if not entry:
            return None
        mcp_id = _inferred_mcp_id(root, entry, origin=origin)
        files = _collect_text_tree(root)
        document = {
            "name": mcp_id,
            "description": (
                f"Imported from {origin}" if origin else f"stdio MCP inferred from {mcp_id} ({entry})"
            ),
            "transport": "stdio",
            "command": "python",
            "args": [entry],
            "env_schema": [],
        }
        if "pyproject.toml" in files:
            # Prefer uv for pyproject (build backends, editable installs).
            document["install"] = {"kind": "uv", "manifest": "pyproject.toml"}
        elif "requirements.txt" in files:
            document["install"] = {"kind": "pip", "manifest": "requirements.txt"}
        return self._upsert(document, files=files)

    def _from_npm_package(self, root: Path, *, origin: str = "") -> str:
        package = root / "package.json"
        try:
            data = json.loads(package.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid package.json: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("package.json must be an object")
        name = _slug_from_package(str(data.get("name") or root.name))
        description = str(data.get("description") or "").strip() or (
            f"MCP server from {origin or root.name}"
        )
        command, args = _npm_entry(data)
        files = _collect_text_tree(root)
        document = {
            "name": name,
            "description": description,
            "transport": "stdio",
            "command": command,
            "args": list(args),
            "install": {"kind": "npm", "manifest": "package.json"},
            "env_schema": [],
        }
        return self._upsert(document, files=files)

    def _upsert(self, document: dict[str, Any], *, files: dict[str, str] | None) -> str:
        # Validate before touching the store.
        normalized = _normalize_import(document)
        try:
            specs = parse_mcp_document(normalized)
        except McpParseError as exc:
            raise ValueError(str(exc)) from exc
        if len(specs) != 1:
            raise ValueError("install accepts one MCP server at a time")
        record = _record_from_spec(specs[0], files=files or {})
        saved = self._store.upsert_mcp(record)
        return saved.mcp_id


def _record_from_spec(spec: McpSpec, *, files: dict[str, str]) -> McpRecord:
    return McpRecord(
        mcp_id=spec.mcp_id,
        description=spec.description,
        transport=spec.transport,
        command=spec.command,
        args=spec.args,
        cwd=spec.cwd,
        install=spec.install,
        env_schema_full=spec.env_schema_full,
        reference_hosts=spec.reference_hosts,
        files=files,
    )


def _safe_dir(raw: str) -> Path:
    path = Path(raw).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"not a directory: {path}")
    return path


def _mcp_upload_rel(rel: str) -> str | None:
    """Drop hidden upload paths; rewrite ``.mcp.json`` → ``mcp.json``."""
    text = str(rel or "").replace("\\", "/").strip("/")
    if not text:
        return None
    parts = [p for p in text.split("/") if p and p not in {".", ".."}]
    if not parts or ".." in text.split("/"):
        return None
    # Hidden parents (``.cursor/…``) are never kept.
    if any(p.startswith(".") for p in parts[:-1]):
        return None
    leaf = parts[-1]
    if leaf == ".mcp.json":
        parts[-1] = "mcp.json"
    elif leaf.startswith("."):
        # ``.env`` and other hidden files are never installed.
        return None
    rewritten = "/".join(parts)
    if skip_rel_path(rewritten, skip_hidden=True):
        return None
    return rewritten


def _unwrap_upload_root(root: Path) -> Path:
    """If the upload is ``FolderName/…`` only, treat ``FolderName`` as the pack root."""
    try:
        children = [c for c in root.iterdir() if c.name not in {".", ".."}]
    except OSError:
        return root
    visible = [c for c in children if not skip_dir_name(c.name, skip_hidden=True)]
    if len(visible) == 1 and visible[0].is_dir():
        return visible[0]
    return root


def _find_manifests(root: Path) -> list[Path]:
    """Locate MCP manifests. May *read* hidden ``.mcp.json``; never walks into hidden dirs."""
    # One manifest per pack, in priority order: ``mcp.json`` and ``.mcp.json``
    # usually describe the same server, and ``server.json`` may be registry
    # metadata without a ``command``.
    for name in _MANIFEST_NAMES:
        direct = root / name
        if direct.is_file():
            return [direct]
    found: list[Path] = []
    # One level of nested packs (monorepo) — non-hidden dirs only.
    try:
        children = sorted(root.iterdir())
    except OSError:
        return []
    for child in children:
        if not child.is_dir() or skip_dir_name(child.name, skip_hidden=True):
            continue
        for name in _MANIFEST_NAMES:
            # Nested hidden manifests (``.mcp.json``) are skipped; use mcp.json.
            if name.startswith("."):
                continue
            candidate = child / name
            if candidate.is_file():
                found.append(candidate)
                break
    return found


def _repo_name_from_origin(origin: str) -> str:
    """Repo/package name from an origin label like ``git:https://host/owner/repo.git``."""
    text = str(origin or "").strip()
    if text.startswith("git:"):
        text = text[4:]
    text = text.rstrip("/")
    if text.endswith(".git"):
        text = text[: -len(".git")]
    return text.rsplit("/", 1)[-1] if "/" in text else ""


def _inferred_mcp_id(root: Path, entry: str, *, origin: str = "") -> str:
    """Prefer a real pack/package name over temp clone/upload directory names.

    A git clone always lands at ``.../repo`` and an upload at
    ``mcp-drop-<random>`` — neither says anything about the actual package, so
    both fall through to ``origin`` (the git URL) or the package/entry-name
    heuristics below.
    """
    base = Path(root)
    name = base.name
    if (
        name not in _GENERIC_PACK_DIR_NAMES
        and not name.startswith("mcp-drop-")
        and not name.startswith("tmp")
    ):
        return _slug_from_package(name)
    repo_name = _repo_name_from_origin(origin)
    if repo_name:
        return _slug_from_package(repo_name)
    packages = [
        child.name
        for child in base.iterdir()
        if child.is_dir()
        and (child / "__init__.py").is_file()
        and not skip_dir_name(child.name, skip_hidden=True)
    ]
    if len(packages) == 1:
        return _slug_from_package(packages[0])
    stem = Path(entry).stem.replace("_", "-")
    return _slug_from_package(stem or name)


def _npm_entry(data: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    """Prefer a published bin name; fall back to ``node <main>``."""
    raw = data.get("bin")
    if isinstance(raw, str) and raw.strip():
        rel = raw.strip().replace("\\", "/")
        base = Path(rel).name
        if "." in base:
            return "node", (rel.lstrip("./"),)
        return base, ()
    if isinstance(raw, dict):
        for key, value in raw.items():
            name = str(key or "").strip().split("/")[-1]
            if name and "." not in name and SKILL_NAME_RE.match(name):
                return name, ()
            if isinstance(value, str) and value.strip():
                rel = value.strip().replace("\\", "/")
                return "node", (rel.lstrip("./"),)
    main = str(data.get("main") or "index.js").strip() or "index.js"
    return "node", (main,)


def _slug_from_package(package: str) -> str:
    text = str(package or "").strip().lower()
    text = text.removeprefix("@").replace("/", "-").replace("_", "-")
    text = re.sub(r"[^a-z0-9-]+", "-", text).strip("-")
    if not text or not SKILL_NAME_RE.match(text):
        raise ValueError(f"could not derive mcp id from {package!r}")
    return text[:128]


def _collect_text_tree(root: Path) -> dict[str, str]:
    """Collect pack text files. Skips junk dirs and every hidden path (``.env``, ``.mcp.json``, …)."""
    out: dict[str, str] = {}
    base = Path(root)
    for path in sorted(base.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(base).as_posix()
        if skip_rel_path(rel, skip_hidden=True):
            continue
        try:
            if path.stat().st_size > 512_000:
                continue
            out[rel] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return out


def _normalize_import(document: dict[str, Any]) -> dict[str, Any]:
    """Adapt Claude Desktop / host configs to this sandbox's catalog shape."""
    out = dict(document)
    command = out.get("command")
    if isinstance(command, list) and command:
        first, *rest = command
        if isinstance(first, str):
            text = first.strip()
            if text.startswith(("/", "~")) or "/" in text or "\\" in text:
                first = Path(text).name
            out["command"] = first
            existing = out.get("args")
            if not isinstance(existing, list):
                existing = []
            # Drop absolute host paths from argv; keep relative/bare tokens.
            cleaned_rest = []
            for item in (*rest, *existing):
                if not isinstance(item, str):
                    continue
                token = item.strip()
                if token.startswith(("/", "~")):
                    continue
                cleaned_rest.append(token)
            out["args"] = cleaned_rest
    elif isinstance(command, str):
        text = command.strip()
        if text.startswith(("/", "~")) or "/" in text or "\\" in text:
            out["command"] = Path(text).name
    env = out.get("env")
    if isinstance(env, dict):
        # Catalog stores names only — values belong on the caller's side.
        schema = out.get("env_schema")
        if not schema:
            out["env_schema"] = [
                {"name": str(key), "required": True}
                for key, value in env.items()
                if str(key).strip()
            ]
        out.pop("env", None)
    return out
