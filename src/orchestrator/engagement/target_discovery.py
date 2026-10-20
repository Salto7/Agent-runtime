"""Asset discovery from findings and LLM suggestions."""

from __future__ import annotations

from typing import Any

from orchestrator.crew.roles.registry import RoleRegistry
from orchestrator.utils.llm import chat_json, llm_configured
from orchestrator.engagement.target_shapes import (
    accept_asset,
    canonical_asset_key,
    coerce_target,
    coerce_targets,
    detect_shape,
    format_service,
    parse_service,
    sanitize_label,
    url_host,
)


def _active_probe_role_ids() -> frozenset[str]:
    """Technical roles that probe targets (RoE owned by the engagement)."""
    try:
        return frozenset(
            r.id for r in RoleRegistry.shared().list_roles() if r.is_technical
        )
    except Exception:
        return frozenset()


_LLM_ASSET_SYSTEM = """You extract engagement subjects for Peon (authorized analysis).
Return JSON only: {"assets":[{"type":"<slug>","value":"<string>"}]}
type is a free-form lowercase slug from the operator's wording (examples: ip, fqdn,
url, netblock, file, malware, sample, source, repo, package, hash, email,
asset, person). Prefer type \"asset\" for a bare organization or brand name.
Only include subjects clearly named. No speculation.
If none, return {"assets":[]}."""


def _expand_related_assets(asset: dict[str, str]) -> list[dict[str, str]]:
    """Lightweight fan-out: URL → host; service → host (graph tips, not taxonomy)."""
    typ = asset.get("type") or ""
    value = asset.get("value") or ""
    out = [asset]
    shape = detect_shape(value)
    if typ == "url" or shape == "url":
        host = url_host(value)
        if host:
            related = accept_asset(host, "host")
            if related:
                out.append(related)
    elif typ == "service" or shape == "service":
        endpoint = parse_service(value)
        if endpoint:
            related = accept_asset(endpoint.host) or accept_asset(endpoint.host, "host")
            if related:
                out.append(related)
    return out


def _normalize_subjects(raw: Any) -> list[dict[str, str]]:
    """Coerce an explicit subjects list into typed {type,value[,relation]} rows."""
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw:
        if isinstance(item, str):
            coerced = accept_asset(item.strip()) or coerce_target(
                {"type": "other", "value": item.strip()}
            )
        elif isinstance(item, dict):
            coerced = coerce_target(item)
        else:
            continue
        if not coerced or not coerced.get("value"):
            continue
        rel_raw = (
            str(item.get("relation") or "")
            if isinstance(item, dict)
            else ""
        )
        row = {
            "type": sanitize_label(str(coerced.get("type") or ""), default="other")
            or "other",
            "value": str(coerced["value"]).strip()[:1024],
            "relation": sanitize_label(rel_raw, default="affected", max_len=32)
            or "affected",
        }
        if row["value"]:
            out.append(row)

    by_key: dict[str, dict[str, str]] = {}
    order: list[str] = []
    for row in out:
        for asset in _expand_related_assets(row):
            relation = (
                row["relation"]
                if asset["value"] == row["value"]
                else "parent"
            )
            canonical = {
                "type": str(asset.get("type") or "other"),
                "value": str(asset.get("value") or ""),
                "relation": relation,
            }
            key = canonical_asset_key(canonical["type"], canonical["value"])
            if key not in by_key:
                order.append(key)
                by_key[key] = canonical
            elif by_key[key].get("relation") == "parent" and relation != "parent":
                by_key[key] = canonical
    return [by_key[key] for key in order]


def _subjects_from_finding_fields(row: dict[str, Any] | None) -> list[dict[str, str]]:
    """Build engagement subjects from structured finding fields.

    Provenance fields (``evidence``, ``evidence_path``) are never subjects.
    Explicit ``subjects`` / ``metadata.subjects`` / ``metadata.assets`` win.
    Legacy ``host`` / ``host+port`` remain valid subject producers.
    """
    if not isinstance(row, dict):
        return []

    explicit = row.get("subjects")
    if not explicit and isinstance(row.get("metadata"), dict):
        meta = row["metadata"]
        explicit = meta.get("subjects") or meta.get("assets") or meta.get(
            "discovered_assets"
        )
    if explicit:
        return _normalize_subjects(explicit)

    found: list[dict[str, str]] = []

    def _push(typ: str, value: str) -> None:
        accepted = accept_asset(value, typ)
        if not accepted:
            return
        for asset in _expand_related_assets(accepted):
            found.append(
                {
                    "type": str(asset.get("type") or typ or "other"),
                    "value": str(asset.get("value") or ""),
                    "relation": "affected",
                }
            )

    host = str(row.get("host") or "").strip()
    asset_type = sanitize_label(str(row.get("asset_type") or ""), default="")
    port = row.get("port")
    protocol = sanitize_label(str(row.get("protocol") or ""), default="", max_len=16)
    if host:
        _push(asset_type or "host", host)
        try:
            port_i = int(port) if port is not None and str(port).strip() != "" else None
        except (TypeError, ValueError):
            port_i = None
        if port_i is not None and 1 <= port_i <= 65535:
            service = format_service(host, port_i, protocol)
            if service:
                _push("service", service)

    # Explicit engagement URLs / digests / packages only — never evidence_path.
    for key, default_typ in (
        ("url", "url"),
        ("uri", "url"),
        ("repo", "repo"),
        ("hash", "hash"),
        ("sample", "sample"),
        ("malware", "malware"),
        ("package", "package"),
    ):
        raw = row.get(key)
        if raw:
            _push(default_typ, str(raw))

    meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    for key in ("urls", "uris", "hashes", "samples", "targets", "candidates"):
        raw = meta.get(key)
        if isinstance(raw, str):
            default = (
                "hash"
                if key == "hashes"
                else "url"
                if key in {"urls", "uris"}
                else asset_type or "other"
            )
            _push(default, raw)
            continue
        if not isinstance(raw, list):
            continue
        for item in raw:
            if isinstance(item, str):
                default = (
                    "hash"
                    if key == "hashes"
                    else "url"
                    if key in {"urls", "uris"}
                    else asset_type or "other"
                )
                _push(default, item)
                continue
            coerced = coerce_target(item)
            if coerced:
                _push(str(coerced.get("type") or ""), str(coerced.get("value") or ""))

    for key, default_typ in (
        ("hash", "hash"),
        ("sha256", "hash"),
        ("md5", "hash"),
        ("sample", "sample"),
        ("malware", "malware"),
        ("repo", "repo"),
        ("url", "url"),
        ("uri", "url"),
        ("subject", asset_type or "other"),
    ):
        raw = meta.get(key)
        if raw:
            _push(default_typ if default_typ != "other" else asset_type or "other", str(raw))

    return _normalize_subjects(found)


class SubjectCanonicalizer:
    """Single ingress contract for persisted findings, RoE, and graph assets."""

    @staticmethod
    def expand(asset: dict[str, str]) -> list[dict[str, str]]:
        return _expand_related_assets(asset)

    @staticmethod
    def normalize(raw: Any) -> list[dict[str, str]]:
        return _normalize_subjects(raw)

    @staticmethod
    def from_finding(row: dict[str, Any] | None) -> list[dict[str, str]]:
        """Canonical subjects only; provenance fields are never considered."""
        return _subjects_from_finding_fields(row)

    @staticmethod
    def active_probe_role_ids() -> frozenset[str]:
        return _active_probe_role_ids()

    @staticmethod
    def suggest_assets(text: str) -> list[dict[str, str]]:
        return _llm_suggest_assets(text)


def _llm_suggest_assets(text: str) -> list[dict[str, str]]:
    """LLM as a discovery producer — returns typed assets; peon trusts the types."""
    try:
        if not llm_configured():
            return []
        from orchestrator.utils import llm_usage

        with llm_usage.purpose("target_discovery"):
            data = chat_json(_LLM_ASSET_SYSTEM, (text or "")[:4000])
    except Exception:
        return []
    if not isinstance(data, dict):
        return []
    raw = data.get("assets")
    if not isinstance(raw, list):
        return []
    found: list[dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        coerced = coerce_target(item)
        if coerced and coerced.get("value"):
            found.append(coerced)
    return coerce_targets(found)
