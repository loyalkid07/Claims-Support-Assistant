import pytest

from agent import (
    AGENT_NAME,
    HANDOFF_INTRO,
    INSTRUCTIONS,
    LLM_MODEL_ID,
    OPENING,
    PERSONA_NAME,
    PROMPT_VERSION,
    STT_MODEL_ID,
    TTS_MODEL_ID,
    TTS_VOICE_ID,
    ClaimsAssistant,
    finish_handoff_segment,
)
from postcall import CompletionRunner
from session_state import SessionState


def test_voice_configuration_is_explicit() -> None:
    assert AGENT_NAME == "claims-support-assistant"
    assert STT_MODEL_ID == "deepgram/nova-3"
    assert LLM_MODEL_ID == "openai/gpt-4.1-mini"
    assert TTS_MODEL_ID == "cartesia/sonic-3"
    assert TTS_VOICE_ID
    assert PROMPT_VERSION == "prompt-v2"
    assert PERSONA_NAME == "Lucia"
    assert f"I'm {PERSONA_NAME}, your automated claims assistant" in OPENING
    assert f"You are {PERSONA_NAME}," in INSTRUCTIONS
    assert "How can I help today?" in OPENING
    assert "may be recorded" not in OPENING
    assert "test details" not in OPENING
    assert "Would you like to continue?" not in OPENING
    assert "only get_claim_status supplies claim facts" in INSTRUCTIONS
    assert "please say yes or no" in INSTRUCTIONS.lower()
    assert "Do not ask for a reason or require verification" in INSTRUCTIONS


def test_claims_assistant_has_guarded_tools() -> None:
    assert hasattr(ClaimsAssistant, "get_claim_status")
    assert not hasattr(ClaimsAssistant, "set_consent")


@pytest.mark.asyncio
async def test_handoff_closes_ai_segment_and_persists_after_intro() -> None:
    order = []
    state = SessionState(room_name="synthetic-room")

    class FakeSpeech:
        async def wait_for_playout(self):
            order.append("intro_played")

    class FakeSession:
        def say(self, message):
            assert message == HANDOFF_INTRO
            order.append("intro_started")
            return FakeSpeech()

        def shutdown(self, *, drain):
            assert drain
            order.append("agent_shutdown")

    async def complete():
        state.end()
        order.append("persist_ai_segment")

    runner = CompletionRunner(complete)
    await finish_handoff_segment(FakeSession(), runner)
    await runner.run()  # Later job shutdown must not write again.
    assert order == [
        "intro_started",
        "intro_played",
        "agent_shutdown",
        "persist_ai_segment",
    ]
    assert state.ended_at_utc is not None
