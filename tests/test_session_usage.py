import pytest
from conftest import trace
from musclememory.core import DomainError, SessionUsageInput


def test_usage_immutable_unknowns_and_scope(service, principal):
    trace(service, principal)
    receipt = SessionUsageInput(
        source="codex-cli",
        model="recorded-model",
        input_tokens=1000,
        cached_input_tokens=800,
        output_tokens=100,
        tool_calls=1,
        elapsed_ms=100,
        capture_complete=True,
        phase="training",
    )
    service.record_session_usage(principal, "training", receipt)
    # Old stored receipts predate the optional captured-action count.
    from musclememory.storage import SessionUsage

    with service.db.transaction() as session:
        row = session.get(SessionUsage, (principal.tenant, "training"))
        row.measurements = {k: v for k, v in row.measurements.items() if k != "captured_tool_calls"}
        session.commit()
    service.record_session_usage(principal, "training", receipt)
    rows = service.session_usage(principal, "team")["items"]
    assert len(rows) == 1 and rows[0]["billed_cost_usd"] is None
    assert rows[0]["estimated_api_cost_usd"] is None
    with pytest.raises(DomainError, match="immutable"):
        service.record_session_usage(
            principal, "training", receipt.model_copy(update={"output_tokens": 99})
        )
    other = service.authenticate(
        service.issue_key("other", {"team": ["read", "write"]})["api_key"], False
    )
    assert service.session_usage(other, "team")["items"] == []
    with pytest.raises(DomainError):
        service.record_session_usage(other, "training", receipt)


def test_usage_validates_cached_and_cost_sources():
    for fields in (
        {"input_tokens": 10, "cached_input_tokens": 20},
        {"billed_cost_usd": 1},
        {"estimated_api_cost_usd": 1},
        {"comparison_group": "pair"},
    ):
        with pytest.raises(ValueError):
            SessionUsageInput(source="client", **fields)
