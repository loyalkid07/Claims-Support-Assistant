import pytest

from claim_delivery import claim_detail, claim_headline
from errors import PolicyError


@pytest.fixture
def claim() -> dict:
    return {
        "claim_id_display": "CLM-482731",
        "status": "Awaiting documents",
        "next_action": "Send the repair estimate",
        "required_documents": ["Repair estimate"],
        "submission": {"method": "SECURE_UPLOAD_ALREADY_ISSUED"},
        "document_receipt": {"status": "PARTIAL", "received_at": None},
        "last_updated_date": "2026-09-26",
        "assigned_representative": {"name": "Taylor", "contact": "555-0100"},
    }


def test_headline_does_not_disclose_detail_fields(claim: dict) -> None:
    result = claim_headline(claim)

    assert result == {
        "result": "found",
        "claim_status": "Awaiting documents",
    }
    assert "Send the repair estimate" not in str(result)
    assert "Taylor" not in str(result)


def test_detail_returns_only_requested_topic_from_committed_schema(claim: dict) -> None:
    assert claim_detail(claim, "next_step") == {
        "result": "found",
        "topic": "next_step",
        "detail": {"next_action": "Send the repair estimate"},
    }
    assert claim_detail(claim, "claim_context")["detail"] == {
        "claim_id_display": "CLM-482731",
        "last_updated_date": "2026-09-26",
    }
    assert claim_detail(claim, "representative")["detail"] == {
        "assigned_representative": {"name": "Taylor", "contact": "555-0100"}
    }


def test_unrecognized_detail_topic_is_rejected(claim: dict) -> None:
    with pytest.raises(PolicyError) as error:
        claim_detail(claim, "payment_amount")
    assert error.value.code == "INVALID_INPUT"


def test_missing_assigned_representative_is_explicit(claim: dict) -> None:
    claim["assigned_representative"] = None
    assert claim_detail(claim, "representative") == {
        "result": "found",
        "topic": "representative",
        "detail": {"assigned_representative": None},
    }
