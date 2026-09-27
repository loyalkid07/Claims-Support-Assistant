import httpx
import pytest

from errors import PolicyError
from session_state import SessionState
from supabase_claims import SupabaseClaimsRepository

CUSTOMER = "11111111-1111-4111-8111-111111111111"
CLAIM = "22222222-2222-4222-8222-222222222222"


def _row() -> dict:
    return {
        "claim_id_display": "CLM-482731",
        "status_code": "AWAITING_DOCUMENTS",
        "status_display": "Awaiting documents",
        "last_updated_at": "2026-09-24T12:00:00+00:00",
        "next_action": "Submit a repair estimate and two damage photographs",
        "required_documents": ["Repair estimate", "Two damage photographs"],
        "submission_method": "SECURE_UPLOAD_ALREADY_ISSUED",
        "submission_instructions": "Use the secure upload link previously issued for this test claim.",
        "mailing_fallback_allowed": True,
        "document_receipt_status": "NOT_CONFIRMED",
        "document_received_at": None,
        "assigned_representative_name": None,
        "assigned_representative_contact": None,
        "record_version": 1,
        "internal_notes": "This must never reach the model",
    }


@pytest.mark.asyncio
async def test_lookup_and_claim_read_use_server_refs_and_safe_projection() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["apikey"] == "sb_secret_synthetic-test"
        assert "authorization" not in request.headers
        if request.url.path.endswith("/customers"):
            return httpx.Response(
                200,
                json=[
                    {
                        "customer_id": CUSTOMER,
                        "postal_lookup_hmac": "a" * 64,
                        "is_active": True,
                    }
                ],
            )
        if request.url.params.get("select") == "claim_pk":
            return httpx.Response(200, json=[{"claim_pk": CLAIM}])
        return httpx.Response(200, json=[_row()])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        repo = SupabaseClaimsRepository(
            "https://example.supabase.co", "sb_secret_synthetic-test", client
        )
        candidate = await repo.find_by_phone_token("b" * 64)
        assert candidate is not None and candidate.customer_ref == CUSTOMER
        assert await repo.find_claim_for_candidate(CUSTOMER, "c" * 64) == CLAIM
        state = SessionState(room_name="room")
        with pytest.raises(PolicyError, match="UNAUTHENTICATED"):
            await repo.get_claim_status(state)
        assert len(requests) == 2
        state.accept_consent()
        state.set_candidate(state.new_lookup_generation(), "opaque")
        state.verify(CUSTOMER, CLAIM)
        projection = await repo.get_claim_status(state)
        assert projection["status"] == "Awaiting documents"
        assert projection["required_documents"] == [
            "Repair estimate",
            "Two damage photographs",
        ]
        assert "internal_notes" not in projection
        assert "customer_id" not in projection
        assert requests[-1].url.params["claim_pk"] == f"eq.{CLAIM}"


@pytest.mark.asyncio
async def test_malformed_claim_is_never_speech_ready() -> None:
    row = _row()
    row["required_documents"] = {"unexpected": "object"}

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=[row]))
    ) as client:
        repo = SupabaseClaimsRepository(
            "https://example.supabase.co", "sb_secret_synthetic-test", client
        )
        state = SessionState(room_name="room")
        state.accept_consent()
        state.set_candidate(state.new_lookup_generation(), "opaque")
        state.verify(CUSTOMER, CLAIM)
        with pytest.raises(PolicyError, match="VALIDATION_ERROR"):
            await repo.get_claim_status(state)


@pytest.mark.asyncio
async def test_verified_name_is_postcall_only_and_never_model_supplied() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/customers")
        assert request.url.params["select"] == "display_name"
        assert request.url.params["customer_id"] == f"eq.{CUSTOMER}"
        return httpx.Response(200, json=[{"display_name": "Maya Chen"}])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        repo = SupabaseClaimsRepository(
            "https://example.supabase.co", "sb_secret_synthetic-test", client
        )
        state = SessionState(room_name="room")
        with pytest.raises(PolicyError, match="UNAUTHENTICATED"):
            await repo.get_verified_caller_name(state)
        state.accept_consent()
        state.set_candidate(state.new_lookup_generation(), "opaque")
        state.verify(CUSTOMER, CLAIM)
        state.end()
        assert await repo.get_verified_caller_name(state) == "Maya Chen"


@pytest.mark.asyncio
async def test_malformed_verified_name_cannot_reach_postcall() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json=[{"display_name": "https://unapproved.example"}]
            )
        )
    ) as client:
        repo = SupabaseClaimsRepository(
            "https://example.supabase.co", "sb_secret_synthetic-test", client
        )
        state = SessionState(room_name="room")
        state.accept_consent()
        state.set_candidate(state.new_lookup_generation(), "opaque")
        state.verify(CUSTOMER, CLAIM)
        with pytest.raises(PolicyError, match="VALIDATION_ERROR"):
            await repo.get_verified_caller_name(state)
