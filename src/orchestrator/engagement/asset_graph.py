"""OAM-inspired engagement asset graph store (open types & relations).

Producers (RoE, findings, roles, operator, tools) upsert assets and typed
edges. Types and ``rel`` labels are free-form slugs — the UI must render
unknown kinds without a Python/JS allowlist.

RoE buckets remain the authorization overlay; this graph is the engagement
surface SoT for visualization and future enrichment.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from functools import partial
from typing import Any, Iterable

from orchestrator.engagement.asset_model import (
    GROUPS,
    classify,
    class_by_slug,
    parent_fqdn_candidates,
    relation_status,
    relation_title,
)
from orchestrator.engagement.target_discovery import SubjectCanonicalizer
from orchestrator.engagement.target_shapes import (
    accept_asset,
    canonical_asset_key,
    coerce_targets,
    detect_shape,
    parse_service,
    sanitize_label,
    split_typed_line,
    url_host,
)


logger = logging.getLogger(__name__)

# Sources whose rows are re-derived from RoE/findings on every rebuild. Anything
# else (agents, tools, operator) is durable producer data and survives rebuilds.
_DERIVED_SOURCES = frozenset({"rules_of_engagement", "roe", "finding", ""})
_MAX_SOURCES = 8
_MAX_INGEST_ASSETS = 500
_MAX_INGEST_RELATIONS = 1000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class AssetGraph:
    """The project's graph: ``assets`` and ``relations`` lists, persisted as one JSON blob in the StateStore."""

    def __init__(self, project: "Project") -> None:
        self.project = project
        data = project.store.get_graph(project.pk) or {}
        self.assets: list[dict[str, Any]] = list(data.get("assets") or [])
        self.relations: list[dict[str, Any]] = list(data.get("relations") or [])
        self.updated_at: str = str(data.get("updated_at") or "")

    def save(self) -> None:
        self.updated_at = _now()
        self.project.store.put_graph(
            self.project.pk,
            {"assets": self.assets, "relations": self.relations, "updated_at": self.updated_at},
        )


class Project:
    """What the graph needs to know about a project: id, RoE, finding rows, and the store holding the graph."""

    def __init__(self, project_id: str, store: Any, *, roe: Any = None, findings: Iterable[dict[str, Any]] = ()):
        self.pk = self.id = str(project_id)
        self.store = store
        self.roe = roe
        self._findings = list(findings)

    @property
    def findings(self) -> list[dict[str, Any]]:
        return sorted(self._findings, key=lambda f: (f.get("seq") or 0))


class _Index:
    """In-memory lookup for one graph (O(1) upserts instead of list scans)."""

    __slots__ = ("assets", "relations", "by_id", "by_key", "rel_ids", "n_assets", "n_rels")

    def __init__(self, graph: AssetGraph) -> None:
        self.assets = graph.assets
        self.relations = graph.relations
        self.by_id = {str(a.get("id") or ""): a for a in graph.assets}
        self.by_key = {str(a.get("key") or ""): a for a in graph.assets if a.get("key")}
        self.rel_ids = {str(r.get("id") or ""): r for r in graph.relations}
        self.n_assets = len(graph.assets)
        self.n_rels = len(graph.relations)


def _index(graph: AssetGraph) -> _Index:
    if not isinstance(graph.assets, list):
        graph.assets = []
    if not isinstance(graph.relations, list):
        graph.relations = []
    idx = graph.__dict__.get("_oag_index")
    if (
        idx is None
        or idx.assets is not graph.assets
        or idx.relations is not graph.relations
        or idx.n_assets != len(graph.assets)
        or idx.n_rels != len(graph.relations)
    ):
        idx = _Index(graph)
        graph.__dict__["_oag_index"] = idx
    return idx


def _touch_source(row: dict[str, Any], name: str, now: str) -> None:
    """Record that ``name`` (re)observed this asset (OAM-style provenance)."""
    name = (name or "").strip()[:120]
    sources = list(row.get("sources") or [])
    for entry in sources:
        if entry.get("name") == name:
            if entry.get("last_seen") != now:
                entry["last_seen"] = now
                entry["count"] = int(entry.get("count") or 1) + 1
            break
    else:
        sources.append({"name": name, "first_seen": now, "last_seen": now, "count": 1})
    row["sources"] = sources[-_MAX_SOURCES:]
    row.setdefault("first_seen", now)
    row["last_seen"] = now


def _stable_id(*parts: str) -> str:
    raw = "|".join((p or "").strip().lower() for p in parts)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def asset_key(typ: str, value: str) -> str:
    """Canonical identity key independent of producer label aliases."""
    return canonical_asset_key(typ, value)


def _prefer_type(previous: str, new: str, value: str) -> str:
    """Keep the most precise display type while identity stays canonical."""
    shape = detect_shape(value)
    if shape:
        return shape
    if previous in {"", "other", "host"} and new not in {"", "other"}:
        return new
    return previous or new or shape or "other"


def _get_or_create_graph(project: Project) -> AssetGraph:
    return AssetGraph(project)


def _upsert_asset(
    graph: AssetGraph,
    *,
    typ: str,
    value: str,
    bucket: str = "discovered",
    source: str = "",
    props: dict | None = None,
) -> dict[str, Any] | None:
    """Insert or merge one asset. Returns the stored row (or None if empty)."""
    accepted = accept_asset(value, typ)
    if accepted is None:
        cleaned = (value or "").strip()
        if not cleaned:
            return None
        accepted = {
            "type": sanitize_label(typ, default="other") or "other",
            "value": cleaned[:1024],
        }
    # Intrinsic value shapes are authoritative inside the graph. Producer
    # labels remain useful outside it, but cannot make one IP appear as both
    # ``host`` and ``ip`` nodes.
    typ = detect_shape(accepted["value"]) or accepted["type"]
    value = accepted["value"]
    key = asset_key(typ, value)
    nid = _stable_id("a", key)
    idx = _index(graph)
    now = _now()
    prev = idx.by_key.get(key) or idx.by_id.get(nid)
    bucket_s = sanitize_label(bucket, default="discovered") or "discovered"
    if prev is None:
        row: dict[str, Any] = {
            "id": nid,
            "type": typ,
            "value": value,
            "key": key,
            "bucket": bucket_s,
            "source": (source or "")[:120],
            "props": dict(props or {}),
        }
        _touch_source(row, source, now)
        graph.assets.append(row)
        idx.by_id[nid] = row
        idx.by_key[key] = row
        idx.n_assets = len(graph.assets)
        return row

    # Prefer non-other type; keep richest bucket (in_scope > seed > candidate …)
    prev["type"] = _prefer_type(str(prev.get("type") or ""), typ, value)
    prev["key"] = key
    prev["bucket"] = _prefer_bucket(str(prev.get("bucket") or ""), bucket_s)
    if source:
        prev_src = str(prev.get("source") or "")
        # Upgrade empty / legacy abbreviated source labels.
        if not prev_src or prev_src == "roe":
            prev["source"] = source[:120]
    if source or not prev.get("sources"):
        _touch_source(prev, source or prev.get("source") or "", now)
    merged = dict(prev.get("props") or {})
    merged.update(props or {})
    prev["props"] = merged
    prev["value"] = value
    idx.by_id[str(prev.get("id") or nid)] = prev
    idx.by_key[key] = prev
    return prev


def _compact_graph(graph: AssetGraph) -> None:
    """Merge legacy alias duplicates and rewire relations to canonical ids."""
    old_assets = list(graph.assets or [])
    old_relations = list(graph.relations or [])
    old_hubs = {
        str(a.get("id") or "")
        for a in old_assets
        if (a.get("props") or {}).get("center")
        or (a.get("props") or {}).get("kind") == "hub"
    }
    graph.assets = []
    graph.relations = []
    id_map: dict[str, str] = {}
    for asset in old_assets:
        row = _upsert_asset(
            graph,
            typ=str(asset.get("type") or "other"),
            value=str(asset.get("value") or ""),
            bucket=str(asset.get("bucket") or "discovered"),
            source=str(asset.get("source") or ""),
            props=asset.get("props") if isinstance(asset.get("props"), dict) else None,
        )
        if row is None:
            continue
        id_map[str(asset.get("id") or "")] = str(row["id"])
        # Carry provenance across the merge (compaction is not an observation).
        for src in asset.get("sources") or []:
            if not isinstance(src, dict) or not src.get("name"):
                continue
            have = {e.get("name"): e for e in row.get("sources") or []}
            if src["name"] not in have:
                row.setdefault("sources", []).append(dict(src))
        if asset.get("first_seen"):
            row["first_seen"] = min(
                str(row.get("first_seen") or asset["first_seen"]), str(asset["first_seen"])
            )
        if asset.get("last_seen"):
            row["last_seen"] = max(
                str(row.get("last_seen") or asset["last_seen"]), str(asset["last_seen"])
            )
        if row.get("sources"):
            row["sources"] = row["sources"][-_MAX_SOURCES:]

    bucket_spokes = {"seed", "in_scope", "candidate", "exclusion", "discovered"}
    for edge in old_relations:
        # Remove legacy visual-only hub spokes. Structural edges survive.
        if (
            str(edge.get("source") or "") in old_hubs
            and str(edge.get("rel") or "") in bucket_spokes
        ):
            continue
        sid = id_map.get(str(edge.get("source") or ""), "")
        tid = id_map.get(str(edge.get("target") or ""), "")
        _upsert_relation(
            graph,
            rel=str(edge.get("rel") or "related"),
            source_id=sid,
            target_id=tid,
            props=edge.get("props") if isinstance(edge.get("props"), dict) else None,
        )


def _upsert_relation(
    graph: AssetGraph,
    *,
    rel: str,
    source_id: str,
    target_id: str,
    props: dict | None = None,
    derived: bool = False,
) -> dict[str, Any] | None:
    """Insert a typed, directed edge between two existing assets.

    ``rel`` is a free-form slug (OAM labels are recognised, others are
    ``custom``). Edges whose endpoints are not in the graph are refused rather
    than stored dangling.
    """
    rel_s = sanitize_label(rel, default="related", max_len=48) or "related"
    sid = (source_id or "").strip()
    tid = (target_id or "").strip()
    if not sid or not tid or sid == tid:
        return None
    idx = _index(graph)
    if sid not in idx.by_id or tid not in idx.by_id:
        return None
    rid = _stable_id("r", rel_s, sid, tid)
    extra = dict(props or {})
    if derived:
        extra["derived"] = True
    existing = idx.rel_ids.get(rid)
    if existing is not None:
        if extra:
            merged = dict(existing.get("props") or {})
            merged.update(extra)
            existing["props"] = merged
        return existing
    row = {"id": rid, "rel": rel_s, "source": sid, "target": tid, "props": extra}
    graph.relations.append(row)
    idx.rel_ids[rid] = row
    idx.n_rels = len(graph.relations)
    return row


_derive = partial(_upsert_relation, derived=True)


_BUCKET_RANK = {
    "hub": 100,
    "in_scope": 80,
    "seed": 70,
    "exclusion": 90,   # an excluded value stays excluded even if it is also in scope or a seed
    "candidate": 50,
    "discovered": 40,
    "finding": 30,
    "other": 0,
}


def _prefer_bucket(prev: str, new: str) -> str:
    if _BUCKET_RANK.get(new, 0) >= _BUCKET_RANK.get(prev, 0):
        return new or prev or "discovered"
    return prev or new or "discovered"


def _typed(text: str) -> tuple[str, str] | None:
    """``type:value`` → ``(type, value)``; None for URLs/IPs/services/plain text."""
    text = (text or "").strip()
    if not text or detect_shape(text):
        return None
    return split_typed_line(text)


def _resolve_ref(graph: AssetGraph, ref: Any) -> str:
    """Resolve an edge endpoint to an asset id, or ``""``.

    Accepts an asset id, ``{"type","value"}``, a typed ``"type:value"`` string,
    or a bare value that matches exactly one asset.
    """
    idx = _index(graph)
    if isinstance(ref, dict):
        typ = str(ref.get("type") or ref.get("asset_type") or "")
        val = str(ref.get("value") or ref.get("target") or "").strip()
        if not val:
            return ""
        row = idx.by_key.get(asset_key(typ, val))
        return str(row["id"]) if row else ""
    text = str(ref or "").strip()
    if not text:
        return ""
    if text in idx.by_id:
        return text
    typed = _typed(text)
    if typed:
        row = idx.by_key.get(asset_key(*typed))
        if row:
            return str(row["id"])
    matches = [
        a for a in graph.assets if str(a.get("value") or "").lower() == text.lower()
    ]
    return str(matches[0]["id"]) if len(matches) == 1 else ""


def _ref_asset(ref: Any) -> dict[str, str] | None:
    """Typed endpoint → asset dict so a relation can create its endpoints."""
    if isinstance(ref, dict):
        typ = str(ref.get("type") or ref.get("asset_type") or "")
        val = str(ref.get("value") or ref.get("target") or "").strip()
        return {"type": typ, "value": val} if val else None
    typed = _typed(str(ref or ""))
    if typed:
        return {"type": typed[0], "value": typed[1]}
    bare = str(ref or "").strip()
    if bare and detect_shape(bare):
        return {"type": "", "value": bare}
    return None


def _ingest_assets(
    project: Project,
    assets: Iterable[dict[str, Any] | str],
    *,
    bucket: str = "discovered",
    source: str = "",
    relations: Iterable[dict[str, Any]] | None = None,
    save: bool = True,
    report: dict[str, Any] | None = None,
) -> AssetGraph:
    """Producer API: upsert assets (+ optional relations) into the project graph.

    Relation endpoints may be typed references; a typed endpoint that is not in
    the graph yet is created (a DNS answer creates its IP, as in Amass).
    ``report`` (if given) receives ``assets``/``relations`` counts and
    ``skipped`` reasons.
    """
    graph = _get_or_create_graph(project)
    rep = report if report is not None else {}
    rep.setdefault("assets", 0)
    rep.setdefault("relations", 0)
    rep.setdefault("skipped", [])

    def add(typ: str, value: str, b: str, src: str, props: dict | None) -> dict | None:
        row = _upsert_asset(graph, typ=typ, value=value, bucket=b, source=src, props=props)
        if row is not None:
            rep["assets"] += 1
        return row

    for n, raw in enumerate(assets or []):
        if n >= _MAX_INGEST_ASSETS:
            rep["skipped"].append(f"asset limit {_MAX_INGEST_ASSETS} reached")
            break
        if isinstance(raw, str):
            typed = _typed(raw)
            accepted = (
                accept_asset(typed[1], typed[0])
                if typed
                else accept_asset(raw)
            ) or {"type": "other", "value": raw.strip()}
            if not add(accepted["type"], accepted["value"], bucket, source, None):
                rep["skipped"].append(f"empty asset: {raw[:40]!r}")
        elif isinstance(raw, dict):
            typ = str(raw.get("type") or raw.get("asset_type") or "")
            value = str(raw.get("value") or raw.get("target") or "").strip()
            props = raw.get("props") if isinstance(raw.get("props"), dict) else None
            if not add(
                typ,
                value,
                str(raw.get("bucket") or bucket),
                str(raw.get("source") or source),
                props,
            ):
                rep["skipped"].append(f"empty asset: {str(raw)[:60]}")

    for n, edge in enumerate(relations or []):
        if n >= _MAX_INGEST_RELATIONS:
            rep["skipped"].append(f"relation limit {_MAX_INGEST_RELATIONS} reached")
            break
        if not isinstance(edge, dict):
            continue
        rel = str(edge.get("rel") or edge.get("type") or edge.get("label") or "related")
        ends = []
        for side in (
            edge.get("source", edge.get("from")),
            edge.get("target", edge.get("to")),
        ):
            rid = _resolve_ref(graph, side)
            if not rid:
                created = _ref_asset(side)
                if created:
                    row = add(created["type"], created["value"], bucket, source, None)
                    rid = str(row["id"]) if row else ""
            ends.append(rid)
        sid, tid = ends
        if not sid or not tid:
            rep["skipped"].append(f"unresolved relation endpoint: {rel}")
            continue
        props = dict(edge.get("props") or {}) if isinstance(edge.get("props"), dict) else {}
        if source:
            props.setdefault("source", source[:120])
        if _upsert_relation(
            graph, rel=rel, source_id=sid, target_id=tid, props=props or None
        ):
            rep["relations"] += 1

    if save:
        graph.save()
    return graph


def _ip_in_net(ip_s: str, net_s: str) -> bool:
    try:
        return ipaddress.ip_address(ip_s) in ipaddress.ip_network(net_s, strict=False)
    except ValueError:
        return False


def _basename(path: str) -> str:
    raw = (path or "").replace("\\", "/").rstrip("/")
    return raw.rsplit("/", 1)[-1] if raw else ""


def _enrich_shape_relations(graph: AssetGraph) -> None:
    """Derive structural edges from value shapes (not type allowlists)."""
    assets = list(graph.assets or [])
    by_value: dict[str, list[dict]] = {}
    for a in assets:
        by_value.setdefault(str(a.get("value") or "").lower(), []).append(a)

    nets = [a for a in assets if detect_shape(str(a.get("value") or "")) == "netblock"]
    ips = [a for a in assets if detect_shape(str(a.get("value") or "")) == "ip"]
    urls = [
        a
        for a in assets
        if detect_shape(str(a.get("value") or "")) == "url"
        or sanitize_label(str(a.get("type") or "")) in {"url", "repo"}
    ]
    services = [
        a for a in assets if detect_shape(str(a.get("value") or "")) == "service"
    ]
    pathish = [
        a
        for a in assets
        if ("/" in str(a.get("value") or "") or "\\" in str(a.get("value") or ""))
        and detect_shape(str(a.get("value") or "")) != "url"
    ]
    digests = [a for a in assets if detect_shape(str(a.get("value") or "")) == "hash"]

    for net in nets:
        for ip in ips:
            if _ip_in_net(str(ip.get("value") or ""), str(net.get("value") or "")):
                _derive(
                    graph,
                    rel="contains",
                    source_id=str(net["id"]),
                    target_id=str(ip["id"]),
                )

    for url in urls:
        host = url_host(str(url.get("value") or ""))
        if not host:
            continue
        for peer in by_value.get(host.lower(), []):
            if peer["id"] == url["id"]:
                continue
            rel = (
                "ip_address"
                if detect_shape(str(peer.get("value") or "")) == "ip"
                else "domain"
            )
            _derive(
                graph, rel=rel, source_id=str(url["id"]), target_id=str(peer["id"])
            )

    for svc in services:
        endpoint = parse_service(str(svc.get("value") or ""))
        if endpoint is None:
            continue
        for peer in by_value.get(endpoint.host.lower(), []):
            _derive(
                graph,
                rel="port",
                source_id=str(peer["id"]),
                target_id=str(svc["id"]),
                props={
                    "port": endpoint.port,
                    **({"protocol": endpoint.protocol} if endpoint.protocol else {}),
                },
            )

    by_base: dict[str, list[dict]] = {}
    for art in pathish:
        base = _basename(str(art.get("value") or "")).lower()
        if base and "." in base:
            by_base.setdefault(base, []).append(art)
    for group in by_base.values():
        if len(group) < 2:
            continue
        root = group[0]
        for peer in group[1:]:
            _derive(
                graph,
                rel="same_file",
                source_id=str(root["id"]),
                target_id=str(peer["id"]),
            )

    for digest in digests:
        hv = str(digest.get("value") or "").lower()
        if len(hv) < 32:
            continue
        for art in pathish:
            if hv in str(art.get("value") or "").lower():
                _derive(
                    graph,
                    rel="hashes",
                    source_id=str(digest["id"]),
                    target_id=str(art["id"]),
                )

    # FQDN hierarchy (OAM ``node``): link each name to its nearest known parent.
    fqdns = {
        str(a.get("value") or "").strip().lower().rstrip("."): a
        for a in assets
        if classify(str(a.get("type") or ""), str(a.get("value") or "")).slug == "fqdn"
    }
    for name, child in fqdns.items():
        for parent_name in parent_fqdn_candidates(name):
            parent = fqdns.get(parent_name)
            if parent is not None and parent["id"] != child["id"]:
                _derive(
                    graph,
                    rel="node",
                    source_id=str(parent["id"]),
                    target_id=str(child["id"]),
                )
                break


def _sync_roe(graph: AssetGraph, roe) -> None:
    if roe is None:
        return
    for bucket, attr in (
        ("seed", "seed"),
        ("in_scope", "in_scope"),
        ("candidate", "candidates"),
        ("exclusion", "exclusions"),
    ):
        for t in coerce_targets(getattr(roe, attr, None)):
            for related in SubjectCanonicalizer.expand(t):
                _upsert_asset(
                    graph,
                    typ=str(related.get("type") or "other"),
                    value=str(related.get("value") or ""),
                    bucket=bucket,
                    source="rules_of_engagement",
                )


def _obj(finding):
    """Finding rows are dicts in the store; the helpers below read attributes."""
    return SimpleNamespace(**finding) if isinstance(finding, dict) else finding


def _finding_as_row(finding) -> dict[str, Any]:
    """Normalize a finding row into the discovery-asset dict shape."""
    finding = _obj(finding)
    meta = getattr(finding, "metadata", None)
    if not isinstance(meta, dict):
        meta = {}
    subjects = getattr(finding, "subjects", None)
    if not isinstance(subjects, list):
        subjects = []
    return {
        "host": getattr(finding, "host", None) or "",
        "port": getattr(finding, "port", None),
        "protocol": getattr(finding, "protocol", None) or "",
        # evidence_path is provenance only — discovery ignores it.
        "evidence_path": getattr(finding, "evidence_path", None) or "",
        "asset_type": getattr(finding, "asset_type", None) or "",
        "kind": getattr(finding, "kind", None) or "",
        "title": getattr(finding, "title", None) or "",
        "subjects": subjects,
        "metadata": meta,
        "url": meta.get("url") or meta.get("uri") or "",
        "uri": meta.get("uri") or "",
        "repo": meta.get("repo") or "",
        "hash": meta.get("hash") or meta.get("sha256") or "",
        "sample": meta.get("sample") or meta.get("malware") or "",
        "malware": meta.get("malware") or "",
        "package": meta.get("package") or "",
    }


def _is_pollution_asset(asset: dict[str, Any]) -> bool:
    """FIND-XXX / finding:* nodes are board rows — not attack-surface subjects."""
    aid = str(asset.get("id") or "")
    key = str(asset.get("key") or "")
    value = str(asset.get("value") or "").strip()
    bucket = str(asset.get("bucket") or "")
    typ = str(asset.get("type") or "")
    props = asset.get("props") if isinstance(asset.get("props"), dict) else {}
    if bucket == "finding" or props.get("kind") == "finding":
        return True
    if aid.startswith("finding:") or key.startswith("finding:"):
        return True
    if typ in {"finding", "findings"}:
        return True
    return bool(value.upper().startswith("FIND-") or value.upper() == "FIND")


def _prune_finding_pollution(graph: AssetGraph) -> None:
    """Drop FIND-* nodes and dangling edges from a previously polluted graph."""
    keep: list[dict[str, Any]] = []
    drop_ids: set[str] = set()
    for a in graph.assets or []:
        if _is_pollution_asset(a):
            drop_ids.add(str(a.get("id") or ""))
            continue
        keep.append(a)
    graph.assets = keep
    if not drop_ids:
        return
    graph.relations = [
        e
        for e in (graph.relations or [])
        if str(e.get("source") or "") not in drop_ids
        and str(e.get("target") or "") not in drop_ids
    ]


def _attach_finding_props(row: dict[str, Any], finding) -> None:
    """Annotate an asset with lightweight finding refs (no FIND-* graph nodes)."""
    props = dict(row.get("props") or {})
    refs = list(props.get("finding_refs") or [])
    seq = getattr(finding, "seq", None)
    ref = {
        "seq": seq,
        "title": (getattr(finding, "title", None) or "")[:200],
        "severity": getattr(finding, "severity", "") or "",
        "status": getattr(finding, "status", "") or "",
        "kind": sanitize_label(getattr(finding, "kind", None) or "", default="") or "",
    }
    # De-dupe by seq when present.
    refs = [
        r
        for r in refs
        if not (seq is not None and r.get("seq") == seq)
    ]
    refs.append(ref)
    props["finding_refs"] = refs[-12:]
    # Surface highest severity for UI chips without a FIND node.
    order = ("critical", "high", "medium", "low", "info")
    severities = [str(r.get("severity") or "").lower() for r in props["finding_refs"]]
    for sev in order:
        if sev in severities:
            props["severity"] = sev
            break
    if not props.get("title"):
        props["title"] = ref["title"]
    row["props"] = props


def _sync_finding(graph: AssetGraph, finding) -> None:
    """Promote finding *subjects* onto the graph — never FIND-XXX nodes."""
    finding = _obj(finding)
    finding_row = _finding_as_row(finding)
    subjects = SubjectCanonicalizer.from_finding(finding_row)
    if not subjects:
        return

    for item in subjects:
        typ = str(item.get("type") or "other")
        value = str(item.get("value") or "").strip()
        if not value:
            continue
        # Meaningful discovery stays as discovered (candidates come from RoE).
        row = _upsert_asset(
            graph,
            typ=typ,
            value=value,
            bucket="discovered",
            source="finding",
        )
        if row is None:
            continue
        if str(item.get("relation") or "affected") != "parent":
            _attach_finding_props(row, finding)


def _sync_project_graph(
    project: Project,
    *,
    findings: list | None = None,
    persist: bool = True,
    graph: AssetGraph | None = None,
    restore=None,
    first_seen: dict[str, str] | None = None,
) -> AssetGraph:
    """Rebuild/merge graph from RoE + finding subjects (idempotent upserts)."""
    graph = graph or _get_or_create_graph(project)
    _prune_finding_pollution(graph)
    _compact_graph(graph)
    roe = getattr(project, "roe", None)
    _sync_roe(graph, roe)
    for f in findings or []:
        _sync_finding(graph, f)
    _prune_finding_pollution(graph)
    if restore is not None:
        restore(graph)
    _enrich_shape_relations(graph)

    # Hub: first seed else first in_scope (always the circle-layout center).
    assets = list(graph.assets or [])
    hub = next((a for a in assets if a.get("bucket") == "seed"), None)
    if hub is None:
        hub = next((a for a in assets if a.get("bucket") == "in_scope"), None)
    if hub is not None:
        for a in assets:
            if a.get("id") == hub["id"]:
                props = dict(a.get("props") or {})
                props["kind"] = "hub"
                props["center"] = True
                a["props"] = props
                a["bucket"] = a.get("bucket") or "seed"
    for a in graph.assets or []:
        prior = (first_seen or {}).get(str(a.get("key") or ""))
        if prior:
            a["first_seen"] = min(str(a.get("first_seen") or prior), prior)
    _prune_dangling(graph)
    if persist:
        graph.save()
    return graph


def _view_payload(graph: AssetGraph) -> dict[str, Any]:
    """UI payload: meaningful assets only (no FIND-XXX board rows)."""
    structural_priority = {"port": 100, "contains": 80, "node": 60}
    parent_by_id: dict[str, tuple[int, str]] = {}
    for edge in graph.relations or []:
        rel = str(edge.get("rel") or "")
        priority = structural_priority.get(rel, 0)
        if not priority:
            continue
        child = str(edge.get("target") or "")
        parent = str(edge.get("source") or "")
        current = parent_by_id.get(child)
        if child and parent and child != parent and (
            current is None or priority > current[0]
        ):
            parent_by_id[child] = (priority, parent)

    nodes: list[dict[str, Any]] = []
    types: set[str] = set()
    buckets: set[str] = set()
    rels: set[str] = set()
    class_by_id: dict[str, Any] = {}
    class_counts: dict[str, int] = {}

    for a in graph.assets or []:
        if _is_pollution_asset(a):
            continue
        typ = str(a.get("type") or "other")
        value = str(a.get("value") or "")
        bucket = str(a.get("bucket") or "other")
        props = a.get("props") if isinstance(a.get("props"), dict) else {}
        kind = str(props.get("kind") or "asset")
        raw_source = str(a.get("source") or "")
        if raw_source in {"roe", "rules_of_engagement"}:
            display_source = "Rules of Engagement"
        elif raw_source == "finding":
            display_source = "Finding subject"
        else:
            display_source = raw_source
        cls = classify(typ, value)
        class_by_id[str(a.get("id") or "")] = cls
        class_counts[cls.slug] = class_counts.get(cls.slug, 0) + 1
        types.add(typ)
        buckets.add(bucket)
        refs = props.get("finding_refs") or []
        nodes.append(
            {
                "id": str(a.get("id") or ""),
                "label": value[:48],
                "type": typ,
                "class": cls.slug,
                "class_label": cls.label,
                "oam": cls.oam,
                "group": cls.group,
                "bucket": bucket,
                "kind": kind,
                "center": bool(props.get("center") or kind == "hub"),
                "value": value,
                "source": display_source,
                "sources": [
                    {
                        "name": str(s.get("name") or ""),
                        "first_seen": str(s.get("first_seen") or ""),
                        "last_seen": str(s.get("last_seen") or ""),
                        "count": int(s.get("count") or 1),
                    }
                    for s in (a.get("sources") or [])
                    if isinstance(s, dict)
                ],
                "first_seen": str(a.get("first_seen") or ""),
                "last_seen": str(a.get("last_seen") or ""),
                "title": str(props.get("title") or ""),
                "severity": str(props.get("severity") or ""),
                "status": str(props.get("status") or ""),
                "finding_count": len(refs) if isinstance(refs, list) else 0,
                "parent_id": parent_by_id.get(str(a.get("id") or ""), (0, ""))[1],
                "props": props,
            }
        )

    edges: list[dict[str, Any]] = []
    rel_counts: dict[str, dict[str, Any]] = {}
    degree: dict[str, int] = {}
    node_ids = {n["id"] for n in nodes}
    for e in graph.relations or []:
        sid = str(e.get("source") or "")
        tid = str(e.get("target") or "")
        rel = str(e.get("rel") or "related")
        if sid not in node_ids or tid not in node_ids or sid == tid:
            continue
        status = relation_status(rel, class_by_id[sid], class_by_id[tid])
        rels.add(rel)
        entry = rel_counts.setdefault(
            rel, {"label": rel, "title": relation_title(rel), "count": 0, "status": status}
        )
        entry["count"] += 1
        if status == "unexpected":
            entry["status"] = "unexpected"
        degree[sid] = degree.get(sid, 0) + 1
        degree[tid] = degree.get(tid, 0) + 1
        edges.append(
            {
                "source": sid,
                "target": tid,
                "rel": rel,
                "title": relation_title(rel),
                "status": status,
                "derived": bool((e.get("props") or {}).get("derived")),
            }
        )
    for n in nodes:
        n["degree"] = degree.get(n["id"], 0)

    group_counts: dict[str, int] = {}
    for slug, count in class_counts.items():
        g = class_by_slug(slug).group
        group_counts[g] = group_counts.get(g, 0) + count

    return {
        "nodes": nodes,
        "edges": edges,
        "legend": {
            "types": sorted(types),
            "buckets": sorted(buckets),
            "relations": sorted(rels),
            "groups": [
                {"slug": slug, "label": label, "count": group_counts[slug]}
                for slug, label in GROUPS
                if group_counts.get(slug)
            ],
            "classes": sorted(
                (
                    {
                        "slug": slug,
                        "label": class_by_slug(slug).label,
                        "oam": class_by_slug(slug).oam,
                        "group": class_by_slug(slug).group,
                        "count": n,
                    }
                    for slug, n in class_counts.items()
                ),
                key=lambda c: (-c["count"], c["slug"]),
            ),
            "relation_counts": sorted(rel_counts.values(), key=lambda r: -r["count"]),
        },
        "stats": {"nodes": len(nodes), "edges": len(edges)},
    }


def _is_derived_asset(row: dict[str, Any]) -> bool:
    names = {
        str(s.get("name") or "")
        for s in (row.get("sources") or [])
        if isinstance(s, dict)
    } or {str(row.get("source") or "")}
    return names <= _DERIVED_SOURCES


def _producer_snapshot(graph: AssetGraph):
    """Capture durable producer data before a rebuild wipes derived rows.

    Returns ``(restore, first_seen)``. ``restore(graph)`` re-applies producer
    assets (with their provenance) and relations onto the freshly derived
    graph; edges whose endpoints no longer exist are dropped.
    """
    assets = [dict(a) for a in graph.assets or [] if not _is_derived_asset(a)]
    relations = [
        dict(r)
        for r in graph.relations or []
        if not (r.get("props") or {}).get("derived")
    ]
    first_seen = {
        str(a.get("key") or ""): str(a.get("first_seen") or "")
        for a in graph.assets or []
        if a.get("key") and a.get("first_seen")
    }
    old_ids = {str(a.get("id") or ""): a for a in graph.assets or []}

    def restore(target: AssetGraph) -> None:
        id_map: dict[str, str] = {}
        for a in assets:
            row = _upsert_asset(
                target,
                typ=str(a.get("type") or "other"),
                value=str(a.get("value") or ""),
                bucket=str(a.get("bucket") or "discovered"),
                props=a.get("props") if isinstance(a.get("props"), dict) else None,
            )
            if row is None:
                continue
            id_map[str(a.get("id") or "")] = str(row["id"])
            have = {e.get("name") for e in row.get("sources") or []}
            for src in a.get("sources") or []:
                if isinstance(src, dict) and src.get("name") and src["name"] not in have:
                    row.setdefault("sources", []).append(dict(src))
            row["sources"] = (row.get("sources") or [])[-_MAX_SOURCES:]
            if a.get("source") and not row.get("source"):
                row["source"] = a["source"]
        for r in relations:
            sid, tid = str(r.get("source") or ""), str(r.get("target") or "")
            # Endpoints may be producer rows (remapped) or derived rows that the
            # rebuild just re-created under the same stable key-derived id.
            sid = id_map.get(sid) or sid
            tid = id_map.get(tid) or tid
            if sid not in _index(target).by_id and sid in old_ids:
                old = old_ids[sid]
                row = _upsert_asset(
                    target, typ=str(old.get("type") or "other"),
                    value=str(old.get("value") or ""), bucket="discovered",
                )
                sid = str(row["id"]) if row else ""
            if tid not in _index(target).by_id and tid in old_ids:
                old = old_ids[tid]
                row = _upsert_asset(
                    target, typ=str(old.get("type") or "other"),
                    value=str(old.get("value") or ""), bucket="discovered",
                )
                tid = str(row["id"]) if row else ""
            _upsert_relation(
                target,
                rel=str(r.get("rel") or "related"),
                source_id=sid,
                target_id=tid,
                props=r.get("props") if isinstance(r.get("props"), dict) else None,
            )

    return restore, first_seen


def _prune_dangling(graph: AssetGraph) -> None:
    ids = {str(a.get("id") or "") for a in graph.assets or []}
    graph.relations = [
        r
        for r in graph.relations or []
        if str(r.get("source") or "") in ids and str(r.get("target") or "") in ids
    ]


def validate_graph(graph: AssetGraph) -> list[str]:
    """Integrity problems (duplicate ids/keys, dangling edges); [] when sound."""
    problems: list[str] = []
    ids: dict[str, int] = {}
    keys: dict[str, int] = {}
    for a in graph.assets or []:
        i, k = str(a.get("id") or ""), str(a.get("key") or "")
        ids[i] = ids.get(i, 0) + 1
        keys[k] = keys.get(k, 0) + 1
    problems += [f"duplicate asset id {i}" for i, n in ids.items() if n > 1]
    problems += [f"duplicate asset key {k}" for k, n in keys.items() if n > 1 and k]
    seen: set[str] = set()
    for r in graph.relations or []:
        rid = str(r.get("id") or "")
        if rid in seen:
            problems.append(f"duplicate relation {rid}")
        seen.add(rid)
        for end in ("source", "target"):
            if str(r.get(end) or "") not in ids:
                problems.append(f"dangling {end} on relation {rid}")
    return problems


class AssetGraphEngine:
    """Project-bound canonical asset graph projection and persistence."""

    def __init__(self, project: Project) -> None:
        self.project = project

    @property
    def graph(self) -> AssetGraph:
        return _get_or_create_graph(self.project)

    def ingest(
        self,
        assets: Iterable[dict[str, Any] | str],
        *,
        bucket: str = "discovered",
        source: str = "",
        relations: Iterable[dict[str, Any]] | None = None,
        save: bool = True,
    ) -> AssetGraph:
        return _ingest_assets(
            self.project,
            assets,
            bucket=bucket,
            source=source,
            relations=relations,
            save=save,
        )

    def sync(
        self,
        *,
        findings: list | None = None,
        persist: bool = True,
        rebuild: bool = False,
    ) -> AssetGraph:
        graph = self.graph
        restore = first_seen = None
        if rebuild:
            # Derived rows (RoE/findings) are recomputed; producer data
            # (agents, tools, operator) is durable and re-applied on top.
            restore, first_seen = _producer_snapshot(graph)
            graph.assets = []
            graph.relations = []
        return _sync_project_graph(
            self.project,
            findings=findings,
            persist=persist,
            graph=graph,
            restore=restore,
            first_seen=first_seen,
        )

    def record(
        self,
        assets: Iterable[dict[str, Any] | str] | None = None,
        relations: Iterable[dict[str, Any]] | None = None,
        *,
        source: str,
        bucket: str = "discovered",
    ) -> dict[str, Any]:
        """Producer entry point for agents/tools. Returns an ingest report."""
        report: dict[str, Any] = {}
        graph = _ingest_assets(
            self.project,
            assets or [],
            bucket=bucket,
            source=source,
            relations=relations,
            save=False,
            report=report,
        )
        # Structural edges (subdomain tree, netblock containment, ports) for
        # whatever was just added.
        _enrich_shape_relations(graph)
        graph.save()
        report["nodes"] = len(graph.assets or [])
        report["edges"] = len(graph.relations or [])
        return report

    def validate(self) -> list[str]:
        """Integrity problems (dangling edges, duplicate keys); [] when sound."""
        return validate_graph(self.graph)

    def rebuild(self, *, persist: bool = True) -> AssetGraph:
        return self.sync(findings=self.project.findings, persist=persist, rebuild=True)

    def is_stale(self) -> bool:
        graph = self.graph
        stamps = [str(f.get("at") or f.get("created_at") or "") for f in self.project.findings]
        stamps = [t for t in stamps if t]
        return bool(stamps and graph.updated_at < max(stamps))

    def payload(self, *, fresh: bool = False) -> dict[str, Any]:
        graph = self.rebuild() if fresh and self.is_stale() else self.graph
        return _view_payload(graph)

    def engagement(
        self,
        *,
        findings: list | None = None,
        persist: bool = True,
        rebuild: bool = False,
    ) -> dict[str, Any]:
        graph = self.sync(
            findings=findings,
            persist=persist,
            rebuild=rebuild,
        )
        return _view_payload(graph)
