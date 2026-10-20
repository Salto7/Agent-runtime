"""Agent memory + proposal tables (same SQLite store as the catalog)."""

from __future__ import annotations

from orchestrator.catalog.store import Model, flag, integer, num, text


class MemoryEntry(Model):
    """One durable memory: typed, named, with a one-line hook.

    ``project_id == ""`` is platform-wide memory. Cognee mirrors entries for
    graph recall; this table is the source of truth.
    """

    pk_fields = ("project_id", "name")
    _silent_fields = ("recall_count", "last_recalled_at")   # bookkeeping: must not look like an edit
    project_id = text("")
    name = text("")
    kind = text("fact")  # lesson | feedback | fact | decision | reference
    description = text("")
    body = text("")
    role_id = text("")
    created_by = text("")
    source_job_id = text("")
    status = text("active")  # active | archived
    recall_count = integer(0)
    last_recalled_at = text("")
    created_at = text("")


class CatalogProposal(Model):
    """A suggested role/skill change staged for human review (never auto-applied)."""

    pk_fields = ("id",)
    id = text("")
    kind = text("")  # role | skill
    action = text("")  # create | update
    target_id = text("")
    title = text("")
    rationale = text("")
    payload = text("{}")
    status = text("pending")  # pending | approved | rejected
    decided_at = text("")
    created_at = text("")


class LlmCallLog(Model):
    """One LLM call (cost/token telemetry written by ``utils.llm_usage.SqliteUsageSink``)."""

    pk_fields = ("id",)
    id = text("")
    purpose = text("unlabeled")
    model = text("")
    ok = flag(True)
    project_id = text("")
    job_id = text("")
    role_id = text("")
    prompt_tokens = integer(0)
    completion_tokens = integer(0)
    total_tokens = integer(0)
    cost_usd = num(0.0)
    duration_ms = integer(0)
    error = text("")
    created_at = text("")
