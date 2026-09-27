import asyncio

import pytest

from errors import PolicyError
from identifiers import (
    lookup_token,
    normalize_claim_id,
    normalize_phone,
    normalize_postal,
)
from postcall import CallerCue, CompletionOnce, CompletionRunner, TrustedEvent
from session_state import Auth, SessionState
from verification import CustomerCandidate, IdentityVerifier

SECRET = b"synthetic-test-only-hmac-secret-32-bytes-minimum"


@pytest.mark.asyncio
async def test_completion_runner_shares_task_when_early_waiter_is_cancelled() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def complete():
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()

    runner = CompletionRunner(complete)
    handoff_waiter = asyncio.create_task(runner.run())
    await started.wait()
    shutdown_waiter = asyncio.create_task(runner.run())
    handoff_waiter.cancel()
    release.set()
    await asyncio.gather(handoff_waiter, shutdown_waiter)
    assert calls == 1


class FakeRepository:
    def __init__(self, candidate: CustomerCandidate | None) -> None:
        self.candidate = candidate
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.delay = False

    async def find_by_phone_token(self, token: str) -> CustomerCandidate | None:
        assert len(token) == 64
        self.started.set()
        if self.delay:
            await self.release.wait()
        return self.candidate

    async def find_claim_for_candidate(
        self, customer_ref: str, claim_token: str
    ) -> str | None:
        if (
            self.candidate
            and customer_ref == self.candidate.customer_ref
            and claim_token == lookup_token(SECRET, "claim", "CLM-482731")
        ):
            return "claim-1"
        return None


def candidate() -> CustomerCandidate:
    return CustomerCandidate(
        customer_ref="customer-1",
        postal_lookup_hmac=lookup_token(SECRET, "postal", "95814"),
    )


def verifier(record: CustomerCandidate | None) -> IdentityVerifier:
    state = SessionState(room_name="synthetic-room")
    state.accept_consent()
    return IdentityVerifier(state, FakeRepository(record), SECRET)


def test_normalization_and_rejection() -> None:
    assert normalize_phone("four one five 555 0142") == "+14155550142"
    assert (
        normalize_phone("my number is four fifteen triple five 0142") == "+14155550142"
    )
    assert normalize_claim_id("C L M four eight two seven three one") == "CLM-482731"
    assert normalize_claim_id("claim 482731") == "CLM-482731"
    assert normalize_claim_id("482731") == "CLM-482731"
    assert normalize_claim_id("four eight two seven thirty one") == "CLM-482731"
    assert normalize_postal("nine five eight one four") == "95814"
    for fn, value in [
        (normalize_phone, "415-555-014"),
        (normalize_postal, "95814@demo"),
        (normalize_claim_id, "CLM-ABCDEF"),
    ]:
        with pytest.raises(PolicyError) as error:
            fn(value)
        assert error.value.code == "INVALID_INPUT"


@pytest.mark.asyncio
async def test_early_slots_wait_for_consent_and_confirmation() -> None:
    state = SessionState(room_name="synthetic-room")
    service = IdentityVerifier(state, FakeRepository(candidate()), SECRET)
    with pytest.raises(PolicyError, match="CONSENT_REQUIRED"):
        service.prepare_phone("415-555-0142")
    state.accept_consent()
    assert service.prepare_phone("415-555-0142") == "0142"
    assert service.prepare_bundle("CLM-482731", "95814") == (
        "CLM-482731",
        "95814",
    )
    with pytest.raises(PolicyError, match="CONFIRMATION_REQUIRED"):
        await service.confirm_phone(confirmed=True)
    service.on_caller_turn()
    assert await service.confirm_phone(confirmed=True) == {
        "status": "continue_verification"
    }
    assert await service.confirm_bundle(confirmed=True) == {"status": "verified"}
    assert service.authorized_claim_ref() == "claim-1"
    assert state.attempts_used == 1


@pytest.mark.asyncio
async def test_no_match_and_mismatch_have_identical_public_results() -> None:
    public_results = []
    internal_reasons = []
    for record in [None, candidate()]:
        service = verifier(record)
        service.prepare_phone("4155550142")
        service.on_caller_turn()
        await service.confirm_phone(confirmed=True)
        service.prepare_bundle("CLM-111111", "95814")
        service.on_caller_turn()
        public_results.append(await service.confirm_bundle(confirmed=True))
        internal_reasons.append(service.last_failure_reason)
        with pytest.raises(PolicyError, match="UNAUTHENTICATED"):
            service.authorized_claim_ref()
    assert public_results[0] == public_results[1]
    assert internal_reasons == ["CUSTOMER_NOT_FOUND", "FACTORS_MISMATCH"]


@pytest.mark.asyncio
async def test_correction_is_free_and_third_completed_bundle_locks() -> None:
    service = verifier(candidate())
    service.prepare_phone("4155550142")
    service.on_caller_turn()
    await service.confirm_phone(confirmed=True)
    service.prepare_bundle("CLM-111111", "95814")
    service.on_caller_turn()
    assert await service.confirm_bundle(confirmed=False) == {
        "status": "correction_needed"
    }
    assert service.state.attempts_used == 0
    for expected in [2, 1, 0]:
        service.prepare_bundle("CLM-111111", "95814")
        service.on_caller_turn()
        result = await service.confirm_bundle(confirmed=True)
        assert result["attempts_remaining"] == expected
    assert service.state.auth == Auth.LOCKED
    with pytest.raises(PolicyError, match="AUTH_LOCKED"):
        service.prepare_bundle("CLM-482731", "95814")


@pytest.mark.asyncio
async def test_superseded_lookup_cannot_bind() -> None:
    service = verifier(candidate())
    repo = service.repository
    assert isinstance(repo, FakeRepository)
    repo.delay = True
    service.prepare_phone("4155550142")
    service.on_caller_turn()
    pending = asyncio.create_task(service.confirm_phone(confirmed=True))
    await repo.started.wait()
    service.prepare_phone("4155550199")
    repo.release.set()
    with pytest.raises(PolicyError, match="STALE_REQUEST"):
        await pending
    assert service.state.auth == Auth.UNVERIFIED
    assert service.state.candidate_ref is None


def test_completion_is_repeatable_and_sentiment_uses_caller_evidence() -> None:
    state = SessionState(room_name="synthetic-room")
    state.end()
    events = (TrustedEvent.VERIFICATION_FAILED, TrustedEvent.CALLER_DISCONNECTED)
    builder = CompletionOnce()
    result = builder.build(
        state, events, (CallerCue.FRUSTRATED,), verified_name="Unverified name"
    )
    assert result is builder.build(state, (TrustedEvent.HANDOFF_CONNECTED,))
    assert result.caller_name == "unknown/unverified"
    assert result.sentiment == "negative"
    assert "Unverified name" not in result.summary


def test_verified_name_is_allowed_only_after_server_verification() -> None:
    state = SessionState(room_name="synthetic-room")
    state.accept_consent()
    state.set_candidate(state.new_lookup_generation(), "opaque-candidate")
    state.verify("customer-1", "claim-1")
    state.end()
    result = CompletionOnce().build(
        state, (TrustedEvent.CLAIM_STATUS_PROVIDED,), verified_name="Maya Chen"
    )
    assert result.authenticated is True
    assert result.caller_name == "Maya Chen"
    assert "Maya Chen" not in result.summary


def test_mixed_call_summary_keeps_completed_actions_and_handoff_outcome() -> None:
    state = SessionState(room_name="synthetic-room")
    state.accept_consent()
    state.set_candidate(state.new_lookup_generation(), "opaque-candidate")
    state.verify("customer-1", "claim-1")
    state.end()
    result = CompletionOnce().build(
        state,
        (
            TrustedEvent.FAQ_ANSWERED,
            TrustedEvent.CLAIM_STATUS_PROVIDED,
            TrustedEvent.HANDOFF_REQUESTED,
            TrustedEvent.HANDOFF_UNAVAILABLE,
        ),
        (CallerCue.INFORMATIONAL,),
        verified_name="Maya Chen",
    )
    assert result.outcome == "handoff_unavailable"
    assert "claim status update" in result.summary
    assert "general claims guidance" in result.summary
    assert "representative" in result.summary
    assert result.sentiment == "neutral"
    assert "Maya Chen" not in result.summary


def test_handoff_only_summary_states_the_unavailable_outcome() -> None:
    state = SessionState(room_name="synthetic-room")
    state.end()
    result = CompletionOnce().build(
        state,
        (TrustedEvent.HANDOFF_REQUESTED, TrustedEvent.HANDOFF_UNAVAILABLE),
    )
    assert result.summary == (
        "The caller requested a representative. No supervisor joined the AI segment."
    )
    assert result.outcome == "handoff_unavailable"


def test_model_view_contains_no_internal_references() -> None:
    state = SessionState(room_name="synthetic-room")
    state.accept_consent()
    state.set_candidate(state.new_lookup_generation(), "opaque-candidate")
    assert state.model_view() == {
        "consent": "accepted",
        "verification": "candidate_ready",
        "attempts_remaining": 3,
        "route": "general",
    }
    assert "opaque-candidate" not in str(state.model_view())
