from datetime import datetime, timedelta, timezone

from orchestrator.utils import llm_usage
from orchestrator.utils.llm_usage import InMemoryUsageSink, purpose


class _FakeUsage:
    def __init__(self, prompt_tokens, completion_tokens, total_tokens):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens


class _FakeResponse:
    def __init__(self, usage):
        self.usage = usage
        self.hidden_params = {"response_cost": 0.002}


def test_purpose_tags_calls_and_resets_after_block():
    assert llm_usage.current_purpose() == ""
    with purpose("recon"):
        assert llm_usage.current_purpose() == "recon"
    assert llm_usage.current_purpose() == ""


def test_purpose_nests_and_restores_outer_tag():
    with purpose("outer"):
        with purpose("inner"):
            assert llm_usage.current_purpose() == "inner"
        assert llm_usage.current_purpose() == "outer"


def test_log_success_writes_usage_and_cost_to_sink():
    sink = InMemoryUsageSink()
    llm_usage._sink = sink
    response = _FakeResponse(_FakeUsage(10, 5, 15))
    now = datetime.now(timezone.utc)
    with purpose("test-purpose"):
        llm_usage._log_success({"model": "gpt-4o"}, response, now, now + timedelta(seconds=1))
    assert len(sink.records) == 1
    row = sink.records[0]
    assert row.purpose == "test-purpose"
    assert row.model == "gpt-4o"
    assert row.prompt_tokens == 10
    assert row.completion_tokens == 5
    assert row.total_tokens == 15
    assert row.cost_usd == 0.002
    assert row.ok is True


def test_log_failure_writes_error_row():
    sink = InMemoryUsageSink()
    llm_usage._sink = sink
    now = datetime.now(timezone.utc)
    llm_usage._log_failure(
        {"model": "gpt-4o", "exception": "rate limited"}, None, now, now + timedelta(seconds=1)
    )
    assert len(sink.records) == 1
    row = sink.records[0]
    assert row.ok is False
    assert row.error == "rate limited"


def test_uncalled_purpose_logs_as_unlabeled():
    sink = InMemoryUsageSink()
    llm_usage._sink = sink
    now = datetime.now(timezone.utc)
    llm_usage._log_success({"model": "x"}, _FakeResponse(_FakeUsage(1, 1, 2)), now, now)
    assert sink.records[0].purpose == "unlabeled"


def test_in_memory_sink_summary_groups_by_purpose_and_sorts_by_cost():
    sink = InMemoryUsageSink()
    sink.record(llm_usage.LlmUsageRecord(purpose="a", model="x", ok=True, cost_usd=1.0))
    sink.record(llm_usage.LlmUsageRecord(purpose="a", model="x", ok=False, cost_usd=2.0))
    sink.record(llm_usage.LlmUsageRecord(purpose="b", model="x", ok=True, cost_usd=5.0))
    rows = sink.summary()
    assert rows[0]["purpose"] == "b"
    assert rows[0]["cost_usd"] == 5.0
    assert rows[1]["purpose"] == "a"
    assert rows[1]["calls"] == 2
    assert rows[1]["failed"] == 1


def test_in_memory_sink_summary_excludes_old_records():
    sink = InMemoryUsageSink()
    old = llm_usage.utcnow() - timedelta(hours=48)
    sink.record(llm_usage.LlmUsageRecord(purpose="a", model="x", ok=True, cost_usd=1.0, created_at=old))
    assert sink.summary(since_hours=24) == []
