import pytest

from errors import PolicyError
from faq import TOPICS, FaqSnapshot
from identifiers import lookup_token
from postcall import CallerCue, TrustedEvent, caller_sentiment
from session_state import Auth, SessionState
from verification import CustomerCandidate, IdentityVerifier
from voice_workflow import VoiceWorkflow, _classify_confirmation

SECRET = b"s" * 32
CUSTOMER = "11111111-1111-4111-8111-111111111111"
CLAIM = "22222222-2222-4222-8222-222222222222"


class FakeClaims:
    async def find_by_phone_token(self, token):
        if token == lookup_token(SECRET, "phone", "+14155550142"):
            return CustomerCandidate(CUSTOMER, lookup_token(SECRET, "postal", "94105"))
        return None

    async def find_claim_for_candidate(self, customer_ref, claim_token):
        if customer_ref == CUSTOMER and claim_token == lookup_token(
            SECRET, "claim", "CLM-482731"
        ):
            return CLAIM
        return None

    async def get_claim_status(self, state):
        assert state.require_claim_ref() == CLAIM
        return {"status": "Awaiting documents"}


def _workflow() -> VoiceWorkflow:
    state = SessionState(room_name="synthetic-room")
    claims = FakeClaims()
    rows = [
        {
            "topic_id": topic,
            "version": "faq-v2",
            "answer_text": f"Approved {topic} answer.",
            "access_class": "PUBLIC",
            "effective_from": "2026-09-26",
            "effective_to": None,
            "is_active": True,
        }
        for topic in TOPICS
    ]
    return VoiceWorkflow(
        state,
        IdentityVerifier(state, claims, SECRET),
        claims,
        FaqSnapshot.from_rows(rows),
    )


def test_public_faq_needs_no_verification_or_verbal_continuation() -> None:
    workflow = _workflow()
    with pytest.raises(PolicyError, match="CONSENT_REQUIRED"):
        workflow.prepare_phone("415 555 0142")
    assert workflow.get_faq("office_hours")["status"] == "found"
    assert workflow.get_faq("emergency")["status"] == "found"
    workflow.state.accept_consent()  # Session entry completed; no caller yes turn.
    assert workflow.prepare_phone("415 555 0142")["status"] == "confirm_phone"
    assert TrustedEvent.FAQ_ANSWERED in workflow.events
    assert TrustedEvent.FAQ_OFFICE_HOURS in workflow.events
    assert TrustedEvent.FAQ_EMERGENCY in workflow.events


def test_sentiment_cues_use_caller_turns_without_retaining_text() -> None:
    workflow = _workflow()
    workflow.caller_turn_committed("I need a status update for my test claim")
    assert caller_sentiment(tuple(workflow.caller_cues)) == "neutral"
    workflow.caller_turn_committed("I am frustrated by the delay")
    assert caller_sentiment(tuple(workflow.caller_cues)) == "negative"
    workflow.caller_turn_committed("Thank you, that helps")
    assert caller_sentiment(tuple(workflow.caller_cues)) == "mixed"
    assert CallerCue.FRUSTRATED in workflow.caller_cues
    assert "frustrated by the delay" not in str(workflow.caller_cues)


@pytest.mark.parametrize(
    ("spoken", "expected"),
    [
        ("Yes, that's right", True),
        ("Thats right", True),
        ("Um, yes, that's right", True),
        ("Uh-huh, that's my number", True),
        ("That's me", True),
        ("mm-hmm", True),
        ("You got it", True),
        ("No, that's wrong", False),
        ("Not quite, I meant a different number", False),
        ("Yes, but the ZIP is wrong", False),
        ("No, yes, wait", False),
        ("Yes, but the ZIP is 10001", None),
        ("Sounds good", None),
        ("I think so", None),
        ("That's right?", None),
        ("I need the claim status", None),
    ],
)
def test_confirmation_classifier_is_conservative(
    spoken: str, expected: bool | None
) -> None:
    assert _classify_confirmation(spoken) is expected


@pytest.mark.asyncio
async def test_claim_tool_requires_separate_confirmed_turns() -> None:
    workflow = _workflow()
    workflow.state.accept_consent()
    with pytest.raises(PolicyError, match="UNAUTHENTICATED"):
        await workflow.get_claim_status()
    assert workflow.prepare_phone("my number is 415 555 0142") == {
        "status": "confirm_phone",
        "last_four": "0142",
    }
    assert await workflow.confirm_phone() == {"status": "clarification_needed"}
    workflow.caller_turn_committed("Yes, that's right")
    assert (await workflow.confirm_phone())["status"] == "continue_verification"
    workflow.prepare_bundle("claim 482731", "94105")
    assert await workflow.confirm_bundle() == {"status": "clarification_needed"}
    workflow.caller_turn_committed("correct")
    assert (await workflow.confirm_bundle())["status"] == "verified"
    assert workflow.state.auth == Auth.VERIFIED
    assert (await workflow.get_claim_status())["status"] == "Awaiting documents"
    assert TrustedEvent.CLAIM_STATUS_PROVIDED in workflow.events


@pytest.mark.asyncio
async def test_labeled_claim_and_zip_are_captured_before_phone() -> None:
    workflow = _workflow()
    workflow.state.accept_consent()
    workflow.caller_turn_committed("Claim 482731, ZIP 94105")
    workflow.caller_turn_committed("415 555 0142")
    workflow.prepare_phone("415 555 0142")
    workflow.caller_turn_committed("yes")
    assert (await workflow.confirm_phone())["status"] == "continue_verification"
    workflow.caller_turn_committed("yes")
    assert (await workflow.confirm_bundle())["status"] == "verified"


@pytest.mark.asyncio
async def test_repeated_bundle_preparation_preserves_phone_confirmation() -> None:
    workflow = _workflow()
    workflow.state.accept_consent()
    workflow.prepare_phone("415 555 0142")
    workflow.caller_turn_committed("yes")
    workflow.prepare_bundle("482731", "94105")
    assert (await workflow.confirm_phone())["status"] == "continue_verification"


@pytest.mark.asyncio
async def test_natural_readback_correction_is_not_a_verification_attempt() -> None:
    workflow = _workflow()
    workflow.state.accept_consent()
    workflow.prepare_phone("415 555 0142")
    workflow.caller_turn_committed("Not quite, I meant a different number")
    assert (await workflow.confirm_phone())["status"] == "correction_needed"
    assert workflow.state.attempts_used == 0


@pytest.mark.asyncio
async def test_unclear_readback_does_not_advance_or_consume_an_attempt() -> None:
    workflow = _workflow()
    workflow.state.accept_consent()
    workflow.prepare_phone("415 555 0142")
    workflow.caller_turn_committed("Sounds good")
    assert await workflow.confirm_phone() == {"status": "clarification_needed"}
    assert workflow.state.auth == Auth.UNVERIFIED
    workflow.caller_turn_committed("Um, yes, that's right")
    assert (await workflow.confirm_phone())["status"] == "continue_verification"
    workflow.prepare_bundle("482731", "94105")
    workflow.caller_turn_committed("Yes, but the ZIP is 10001")
    assert await workflow.confirm_bundle() == {"status": "clarification_needed"}
    assert workflow.state.attempts_used == 0
    workflow.caller_turn_committed("No, that ZIP is wrong")
    assert (await workflow.confirm_bundle())["status"] == "correction_needed"
    assert workflow.state.attempts_used == 0


@pytest.mark.asyncio
async def test_unknown_phone_follows_generic_failure_path() -> None:
    workflow = _workflow()
    workflow.state.accept_consent()
    workflow.prepare_phone("4155550199")
    workflow.caller_turn_committed("yes")
    assert (await workflow.confirm_phone())["status"] == "continue_verification"
    workflow.prepare_bundle("482731", "94105")
    workflow.caller_turn_committed("yes")
    result = await workflow.confirm_bundle()
    assert result["status"] == "retry"
    assert "could not verify an account" in result["spoken_message"]
    assert "recheck the phone number" in result["spoken_message"]
    assert workflow.state.auth != Auth.VERIFIED
    assert TrustedEvent.VERIFICATION_FAILED in workflow.events
