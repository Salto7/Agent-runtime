import pytest

from orchestrator.brain.a2a import A2AHub


def test_send_is_directed_not_broadcast():
    hub = A2AHub()
    msg = hub.send(sender_job_id="a", recipient_job_id="b", kind="inform", content="hi")
    assert msg.sender_job_id == "a"
    assert msg.recipient_job_id == "b"
    # A third job never sees it.
    assert hub.inbox("c") == []
    assert [m.id for m in hub.inbox("b")] == [msg.id]


def test_cannot_message_self():
    hub = A2AHub()
    with pytest.raises(ValueError):
        hub.send(sender_job_id="a", recipient_job_id="a", kind="inform", content="hi")


def test_per_pair_cap_stops_a_runaway_loop():
    hub = A2AHub(max_messages_per_pair=3)
    for _ in range(3):
        hub.send(sender_job_id="a", recipient_job_id="b", kind="inform", content="x")
    with pytest.raises(ValueError):
        hub.send(sender_job_id="a", recipient_job_id="b", kind="inform", content="x")
    # The cap is per unordered pair, not per direction.
    with pytest.raises(ValueError):
        hub.send(sender_job_id="b", recipient_job_id="a", kind="inform", content="x")


def test_multisend_is_capped_and_reports_failures_individually():
    hub = A2AHub(max_recipients=2, max_messages_per_pair=1)
    hub.send(sender_job_id="a", recipient_job_id="y", kind="inform", content="already one")

    with pytest.raises(ValueError):
        hub.multisend(sender_job_id="a", recipient_job_ids=["x", "y", "z"], kind="inform", content="hi")

    result = hub.multisend(sender_job_id="a", recipient_job_ids=["x", "y"], kind="inform", content="hi")
    assert [m.recipient_job_id for m in result["sent"]] == ["x"]
    assert "y" in result["failed"]  # already at cap with "a"


def test_wake_callback_fires_on_send_not_on_construction():
    hub = A2AHub()
    woken = []
    hub.send(
        sender_job_id="a",
        recipient_job_id="b",
        kind="handoff",
        content="resume please",
        wake=woken.append,
    )
    assert woken == ["b"]


def test_mark_read_and_status_counters():
    hub = A2AHub()
    msg = hub.send(sender_job_id="a", recipient_job_id="b", kind="inform", content="hi")
    status = hub.status("b")
    assert status == {"sent": 0, "received": 1, "unread": 1}

    hub.mark_read(msg.id)
    status = hub.status("b")
    assert status["unread"] == 0
