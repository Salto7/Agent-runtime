"""Shared engagement operations for capability and CrewAI tool adapters."""

from __future__ import annotations

from typing import Any

from orchestrator.agent.job import get_job


class EngagementTools:
    """One typed bridge contract behind both framework tool surfaces."""

    @staticmethod
    def list_objectives() -> str:
        return get_job().bridge.list_objectives()

    @staticmethod
    def update_objective_status(seq: int, status: str, note: str = "") -> str:
        return get_job().bridge.update_objective_status(seq, status, note)

    @staticmethod
    def ask_operator(
        question: str,
        *,
        options: str = "",
        blocks_job: bool = True,
    ) -> str:
        return get_job().bridge.ask_operator(
            question,
            options=options,
            blocks_job=blocks_job,
        )

    @staticmethod
    def record_finding(**fields: Any) -> str:
        return get_job().bridge.record_finding(**fields)

    @staticmethod
    def record_findings(findings_json: str) -> str:
        return get_job().bridge.record_findings(findings_json)

    @staticmethod
    def recall_memory(query: str) -> str:
        return get_job().bridge.recall_memory(query)

    @staticmethod
    def save_memory(
        name: str, kind: str, description: str, body: str = "", scope: str = "project"
    ) -> str:
        return get_job().bridge.save_memory(name, kind, description, body, scope)

    @staticmethod
    def forget_memory(name: str, scope: str = "project") -> str:
        return get_job().bridge.forget_memory(name, scope)

    @staticmethod
    def send_message(
        to: str, content: str, kind: str = "inform", reply_to: str = ""
    ) -> str:
        return get_job().bridge.send_message(
            to, content, kind=kind, reply_to=reply_to
        )

    @staticmethod
    def check_inbox() -> str:
        return get_job().bridge.check_inbox()

    @staticmethod
    def message_status(limit: int = 10) -> str:
        return get_job().bridge.message_status(limit)

    @staticmethod
    def go_idle(reason: str = "") -> str:
        return get_job().bridge.go_idle(reason)

    @staticmethod
    def list_team() -> str:
        return get_job().bridge.list_team()

    @staticmethod
    def record_assets(assets_json: str, relations_json: str = "") -> str:
        return get_job().bridge.record_assets(assets_json, relations_json)

    @staticmethod
    def list_findings(kind: str = "") -> str:
        return get_job().bridge.list_findings(kind=kind)
