"""REST client for the Cognee Docker sidecar (official HTTP API).

The library does **not** ``import cognee`` in-process: the PyPI package is the memory
engine itself (storage + pipelines). Web + worker share one sidecar writer via
``/api/v1/remember|recall|improve|cognify`` — see https://docs.cognee.ai/api-reference
and https://github.com/topoteretes/cognee. LLM/embeddings for the sidecar are
configured in ``docker-compose.yml`` (OpenRouter + ``LITELLM_MODEL``).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Any

import httpx

from orchestrator import settings
from orchestrator.utils.service import SharedServiceBase

logger = logging.getLogger(__name__)


class CogneeBackend(SharedServiceBase):
    """Thin HTTP façade over Cognee ``/api/v1`` (single-writer sidecar)."""

    def base_url(self) -> str:
        """``COGNEE_URL`` wins; else ``http://<COGNEE_HOST>:<COGNEE_PORT>`` (default localhost:8001)."""
        explicit = settings.get_str("COGNEE_URL")
        if explicit:
            return explicit.rstrip("/")
        port = max(1, min(65535, settings.get_int("COGNEE_PORT", 8001)))
        return f"http://{settings.get_str('COGNEE_HOST', 'localhost')}:{port}"

    def enabled(self) -> bool:
        return settings.cognee_enabled()

    def timeout_s(self) -> float:
        return max(5.0, settings.get_float("COGNEE_HTTP_TIMEOUT_S", 120))

    def health(self) -> dict[str, Any]:
        if not self.enabled():
            return {"ok": False, "enabled": False, "error": "COGNEE_ENABLED=false"}
        try:
            code, body = self._request("GET", "/health")
            return {
                "ok": 200 <= code < 300,
                "enabled": True,
                "status_code": code,
                "body": body if isinstance(body, dict) else {"raw": body},
                "base_url": self.base_url(),
            }
        except Exception as exc:
            return {
                "ok": False,
                "enabled": True,
                "error": str(exc)[:400],
                "base_url": self.base_url(),
            }

    def remember(
        self,
        content: str | dict[str, Any] | list[Any],
        *,
        dataset_name: str,
        session_id: str = "",
        self_improvement: bool = False,
        run_in_background: bool = True,
        document_name: str = "",
    ) -> dict[str, Any]:
        """Ingest text/JSON into Cognee (multipart ``remember``).

        Cognee identifies a document by its filename within the dataset and
        refuses to "update" one (HTTP 409), so distinct documents must carry
        distinct names. ``document_name`` is explicit and stable (a record
        that is rebuilt wholesale); by default the name is derived from the
        content.
        """
        if not self.enabled():
            return {"ok": False, "skipped": True, "reason": "disabled"}
        text = self._normalize_content(content)
        if not text.strip():
            return {"ok": False, "skipped": True, "reason": "empty"}
        ext = "json" if text.lstrip().startswith(("{", "[")) else "txt"
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", str(document_name or "").strip()).strip("-.")[:120]
        if not stem:
            # Content-addressed default: identical content re-adds idempotently,
            # different content never collides on a shared name.
            stem = "memory-" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
        filename = f"{stem}.{ext}"
        fields: dict[str, str] = {
            "datasetName": str(dataset_name or "platform").strip() or "platform",
            "run_in_background": "true" if run_in_background else "false",
            "self_improvement": "true" if self_improvement else "false",
        }
        sid = str(session_id or "").strip()
        if sid:
            fields["session_id"] = sid
        try:
            mime = "application/json" if filename.endswith(".json") else "text/plain"
            code, body = self._request(
                "POST",
                "/api/v1/remember",
                data=fields,
                files={"data": (filename, text.encode("utf-8"), mime)},
            )
            return {"ok": 200 <= code < 300, "status_code": code, "body": body}
        except Exception as exc:
            logger.warning("cognee remember failed: %s", exc)
            return {"ok": False, "error": str(exc)[:400]}

    def recall(
        self,
        query: str,
        *,
        datasets: list[str] | None = None,
        session_id: str = "",
        top_k: int = 8,
        search_type: str = "CHUNKS",
    ) -> dict[str, Any]:
        """Retrieve stored memory.

        Defaults to ``CHUNKS``: the nearest stored documents, verbatim. A
        completion search type lets the sidecar answer from the model's own
        knowledge when the graph is thin, and Cognee caches that answer as a
        Q&A session which a later ``improve`` persists back as "knowledge".
        Request a completion type only for an explicit, operator-driven question.

        The REST payload uses camelCase names (``searchType``, ``topK``,
        ``sessionId``); snake_case names are ignored by the sidecar.
        """
        if not self.enabled():
            return {"ok": False, "skipped": True, "results": [], "text": ""}
        q = (query or "").strip()
        if not q:
            return {"ok": True, "results": [], "text": ""}
        kind = str(search_type or "CHUNKS").strip().upper()
        payload: dict[str, Any] = {
            "query": q,
            "datasets": list(datasets or ["platform"]),
            "searchType": kind,
            "topK": max(1, min(50, int(top_k or 8))),
        }
        sid = str(session_id or "").strip()
        if sid:
            payload["sessionId"] = sid
        try:
            code, body = self._request("POST", "/api/v1/recall", json_body=payload)
            build_err = self._build_failed(body)
            if build_err:
                return {
                    "ok": False,
                    "status_code": code,
                    "body": body,
                    "text": "",
                    "results": body if isinstance(body, list) else [],
                    "error": build_err,
                    "build_failed": True,
                }
            if isinstance(body, list) and kind == "CHUNKS":
                body = [r for r in body if not self._is_session_artifact(r)]
            if not 200 <= code < 300:
                # an error body (e.g. DatasetNotFoundError for a project with no memories yet) is not knowledge
                missing = code == 404 or "not found" in str(body).lower()
                return {"ok": missing, "status_code": code, "body": body, "text": "", "results": [],
                        "error": "" if missing else str(body)[:400]}
            text = self._recall_text(body)
            return {
                "ok": 200 <= code < 300,
                "status_code": code,
                "body": body,
                "text": text,
                "results": body if isinstance(body, list) else [],
            }
        except Exception as exc:
            logger.warning("cognee recall failed: %s", exc)
            return {"ok": False, "error": str(exc)[:400], "text": "", "results": []}

    def improve(
        self,
        *,
        dataset: str = "",
        dataset_id: str = "",
        session_ids: list[str] | None = None,
        run_in_background: bool = True,
    ) -> dict[str, Any]:
        """Call Cognee ``/api/v1/improve`` (expects ``datasetName`` / ``datasetId``)."""
        if not self.enabled():
            return {"ok": False, "skipped": True, "reason": "disabled"}
        name = str(dataset or "").strip()
        did = str(dataset_id or "").strip()
        if not name and not did:
            return {
                "ok": False,
                "error": "datasetName or datasetId required",
            }
        payload: dict[str, Any] = {
            "runInBackground": bool(run_in_background),
        }
        if did:
            payload["datasetId"] = did
        else:
            payload["datasetName"] = name
        ids = [str(s).strip() for s in (session_ids or []) if str(s).strip()]
        if ids:
            payload["sessionIds"] = ids
        try:
            code, body = self._request("POST", "/api/v1/improve", json_body=payload)
            return {
                "ok": 200 <= code < 300,
                "status_code": code,
                "body": body,
                "dataset": name or did,
            }
        except Exception as exc:
            logger.warning("cognee improve failed: %s", exc)
            return {"ok": False, "error": str(exc)[:400]}

    def improve_many(
        self,
        *,
        datasets: list[str],
        session_ids: list[str] | None = None,
        run_in_background: bool = True,
    ) -> dict[str, Any]:
        """Improve each dataset name; aggregate ok/errors."""
        names = [str(d).strip() for d in (datasets or []) if str(d).strip()]
        if not names:
            return {"ok": False, "error": "no datasets", "results": []}
        results: list[dict[str, Any]] = []
        for name in names:
            # Session filter only applies when improving a single dataset.
            sid = session_ids if len(names) == 1 else None
            row = self.improve(
                dataset=name,
                session_ids=sid,
                run_in_background=run_in_background,
            )
            results.append({"dataset": name, **row})
        ok = all(bool(r.get("ok")) for r in results)
        return {"ok": ok, "results": results, "count": len(results)}

    def clear_dataset(self, name: str) -> int:
        """Delete every document in dataset ``name`` (the dataset itself stays).

        Used for datasets the library fully owns and rebuilds from its own records.
        Returns how many documents were removed.
        """
        if not self.enabled():
            return 0
        rows = self.datasets().get("datasets") or []
        did = next(
            (str(r.get("id")) for r in rows if isinstance(r, dict) and r.get("name") == name),
            "",
        )
        if not did:
            return 0
        removed = 0
        try:
            code, body = self._request("GET", f"/api/v1/datasets/{did}/data")
            items = body if isinstance(body, list) else []
            for item in items:
                data_id = str((item or {}).get("id") or "")
                if not data_id:
                    continue
                code, _ = self._request("DELETE", f"/api/v1/datasets/{did}/data/{data_id}")
                removed += 1 if 200 <= code < 300 else 0
        except Exception as exc:  # noqa: BLE001
            logger.warning("cognee clear_dataset(%s) failed: %s", name, exc)
        return removed

    def clear_all(self) -> dict[str, Any]:
        """Delete every dataset (documents, graph and vectors) the sidecar holds."""
        if not self.enabled():
            return {"ok": False, "skipped": True, "reason": "disabled", "deleted": []}
        rows = self.datasets().get("datasets") or []
        deleted: list[str] = []
        errors: list[str] = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            name = str(row.get("name") or row["id"])
            try:
                code, _ = self._request("DELETE", f"/api/v1/datasets/{row['id']}")
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{name}: {str(exc)[:120]}")
                continue
            if 200 <= code < 300:
                deleted.append(name)
            else:
                errors.append(f"{name}: HTTP {code}")
        return {"ok": not errors, "deleted": deleted, "errors": errors}

    def datasets(self) -> dict[str, Any]:
        if not self.enabled():
            return {"ok": False, "datasets": []}
        try:
            code, body = self._request("GET", "/api/v1/datasets")
            rows = body if isinstance(body, list) else (body or {}).get("datasets") or []
            return {"ok": 200 <= code < 300, "status_code": code, "datasets": rows}
        except Exception as exc:
            return {"ok": False, "datasets": [], "error": str(exc)[:400]}

    def sessions(
        self,
        *,
        range_: str = "all",
        limit: int = 200,
        offset: int = 0,
    ) -> dict[str, Any]:
        """List Cognee sessions (paginated)."""
        if not self.enabled():
            return {"ok": False, "sessions": []}
        q = (
            f"?range={str(range_ or 'all').strip() or 'all'}"
            f"&limit={max(1, min(500, int(limit or 200)))}"
            f"&offset={max(0, int(offset or 0))}"
        )
        try:
            code, body = self._request("GET", f"/api/v1/sessions{q}")
            rows = []
            if isinstance(body, dict):
                rows = body.get("sessions") or []
            elif isinstance(body, list):
                rows = body
            return {
                "ok": 200 <= code < 300,
                "status_code": code,
                "sessions": rows if isinstance(rows, list) else [],
                "total": (body or {}).get("total") if isinstance(body, dict) else len(rows),
            }
        except Exception as exc:
            return {"ok": False, "sessions": [], "error": str(exc)[:400]}

    @staticmethod
    def _normalize_content(content: str | dict[str, Any] | list[Any]) -> str:
        if isinstance(content, str):
            return content
        return json.dumps(content, ensure_ascii=False, indent=2, default=str)

    @staticmethod
    def _build_failed(body: Any) -> str:
        """Cognee returns HTTP 200 with structured ``status=build_failed`` rows."""
        rows = body if isinstance(body, list) else []
        if isinstance(body, dict):
            maybe = body.get("results") or body.get("data")
            if isinstance(maybe, list):
                rows = maybe
        for item in rows:
            if not isinstance(item, dict):
                continue
            if str(item.get("status") or "").strip().lower() != "build_failed":
                continue
            msg = str(
                item.get("error_message") or item.get("text") or "memory build failed"
            ).strip()
            return msg[:800]
        return ""

    def cognify(
        self,
        *,
        datasets: list[str] | None = None,
        dataset_ids: list[str] | None = None,
        run_in_background: bool = False,
    ) -> dict[str, Any]:
        """Run Cognee cognify on already-ingested dataset data."""
        if not self.enabled():
            return {"ok": False, "skipped": True, "reason": "disabled"}
        payload: dict[str, Any] = {
            "runInBackground": bool(run_in_background),
        }
        names = [str(s).strip() for s in (datasets or []) if str(s).strip()]
        ids = [str(s).strip() for s in (dataset_ids or []) if str(s).strip()]
        if names:
            payload["datasets"] = names
        if ids:
            payload["datasetIds"] = ids
        if not names and not ids:
            payload["datasets"] = ["platform"]
        try:
            code, body = self._request("POST", "/api/v1/cognify", json_body=payload)
            return {"ok": 200 <= code < 300, "status_code": code, "body": body}
        except Exception as exc:
            logger.warning("cognee cognify failed: %s", exc)
            return {"ok": False, "error": str(exc)[:400]}

    @staticmethod
    def _is_session_artifact(item: Any) -> bool:
        """Cached chat Q&A (``Session ID: ... Question: ... Answer: ...``) is not memory."""
        text = str(item.get("text") or "") if isinstance(item, dict) else ""
        return text.lstrip().startswith("Session ID:") and "Question:" in text

    @staticmethod
    def _recall_text(body: Any) -> str:
        if body is None:
            return ""
        if isinstance(body, str):
            return body.strip()
        if isinstance(body, dict):
            for key in ("answer", "result", "text", "completion", "memory"):
                val = body.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
            results = body.get("results") or body.get("data")
            if isinstance(results, list):
                return CogneeBackend._recall_text(results)
            return json.dumps(body, ensure_ascii=False)[:8000]
        if isinstance(body, list):
            parts: list[str] = []
            for item in body[:20]:
                if isinstance(item, str):
                    parts.append(item.strip())
                elif isinstance(item, dict):
                    if str(item.get("status") or "").strip().lower() == "build_failed":
                        continue
                    for key in ("memory", "text", "content", "answer", "snippet"):
                        val = item.get(key)
                        if isinstance(val, str) and val.strip():
                            parts.append(val.strip())
                            break
                    else:
                        parts.append(json.dumps(item, ensure_ascii=False)[:500])
            return "\n".join(f"- {p}" for p in parts if p)
        return str(body)[:8000]

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
    ) -> tuple[int, Any]:
        """HTTP call to the Cognee sidecar; returns ``(status, json-or-text)``."""
        resp = httpx.request(
            method,
            f"{self.base_url()}{path}",
            json=json_body,
            data=data,
            files=files,
            headers={"Accept": "application/json"},
            timeout=self.timeout_s(),
        )
        if not resp.text.strip():
            return resp.status_code, {}
        try:
            return resp.status_code, resp.json()
        except ValueError:
            return resp.status_code, resp.text
