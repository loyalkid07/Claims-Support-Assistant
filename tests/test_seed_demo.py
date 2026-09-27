"""The synthetic cases form a coherent, bounded claims portfolio."""

from datetime import date, datetime
from uuid import UUID

from scripts.seed_demo import CASES, SECONDARY_CASES, build_rows


def test_claim_cases_are_consistent_and_have_no_unverified_destinations() -> None:
    assert len(CASES) == 6
    assert set(SECONDARY_CASES) == {"maya"}
    for case in [*CASES.values(), *SECONDARY_CASES.values()]:
        assert case["claim_type"] == "AUTO_PHYSICAL_DAMAGE"
        assert (
            date.fromisoformat(case["loss_date"])
            <= datetime.fromisoformat(case["last_updated_at"]).date()
        )
        assert case["mailing_fallback_allowed"] is False
        assert "http" not in case["submission_instructions"].lower()
        if case["required_documents"]:
            assert case["status_code"] == "AWAITING_DOCUMENTS"
            assert case["submission_method"] == "UNKNOWN"
        if case["document_receipt_status"] in {"RECEIVED", "PARTIAL"}:
            assert case["document_received_at"] is not None
        if case["estimated_next_step_at"]:
            assert datetime.fromisoformat(case["estimated_next_step_at"]) >= (
                datetime.fromisoformat(case["last_updated_at"])
            )


def test_seed_has_six_customers_seven_distinct_claims_and_one_multi_claim_account() -> (
    None
):
    factors = {}
    for index, key in enumerate(CASES):
        factors[key] = {
            "customer_id": str(UUID(int=index + 1)),
            "claim_pk": str(UUID(int=index + 101)),
            "display_name": f"Synthetic Caller {index}",
            "phone": f"415555{1000 + index:04d}",
            "postal": f"{90000 + index}",
            "claim_id": f"CLM-{200000 + index}",
        }
    factors["maya"]["secondary_claim_pk"] = str(UUID(int=999))
    factors["maya"]["secondary_claim_id"] = "CLM-299999"
    customers, claims = build_rows(
        factors, b"synthetic-test-only-hmac-secret-32-bytes-minimum"
    )
    assert len(customers) == 6
    assert len(claims) == 7
    assert len({row["claim_pk"] for row in claims}) == 7
    assert len({row["claim_lookup_hmac"] for row in claims}) == 7
    assert (
        sum(row["customer_id"] == factors["maya"]["customer_id"] for row in claims) == 2
    )
    assert all("phone" not in row and "postal" not in row for row in customers)
