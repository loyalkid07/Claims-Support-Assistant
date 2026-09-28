from types import SimpleNamespace

import pytest

from agent import (
    AGENT_NAME,
    GOODBYE,
    HANDOFF_INTRO,
    INSTRUCTIONS,
    LLM_MODEL_ID,
    OPENING,
    PERSONA_NAME,
    PROMPT_VERSION,
    STT_MODEL_ID,
    TTS_MODEL_ID,
    TTS_VOICE_ID,
    WORKFLOW_VERSION,
    ClaimsAssistant,
    finish_handoff_segment,
    finish_normal_call,
)
from postcall import CompletionRunner, TrustedEvent
from session_state import CallPhase, SessionState


def test_voice_configuration_is_explicit() -> None:
    assert AGENT_NAME == "claims-support-assistant"
    assert STT_MODEL_ID == "deepgram/nova-3"
    assert LLM_MODEL_ID == "openai/gpt-4.1-mini"
    assert TTS_MODEL_ID == "cartesia/sonic-3"
    assert TTS_VOICE_ID
    assert PROMPT_VERSION == "prompt-v4"
    assert WORKFLOW_VERSION == "workflow-v4"
    assert PERSONA_NAME == "Lucia"
    assert f"I'm {PERSONA_NAME}, your automated claims assistant" in OPENING
    assert f"You are {PERSONA_NAME}," in INSTRUCTIONS
    assert "How can I help today?" in OPENING
    assert "may be recorded" not in OPENING
    assert "test details" not in OPENING
    assert "Would you like to continue?" not in OPENING
    assert (
        "say the stored status first, then briefly state its recorded next action"
        in INSTRUCTIONS
    )
    assert "please say yes or no" in INSTRUCTIONS.lower()
    assert "Do not ask for a reason or require verification" in INSTRUCTIONS
    assert "Call end_call only when the caller clearly says" in INSTRUCTIONS


def test_claims_assistant_has_guarded_tools() -> None:
    assert hasattr(ClaimsAssistant, "get_claim_status")
    assert hasattr(ClaimsAssistant, "get_claim_detail")
    assert hasattr(ClaimsAssistant, "end_call")
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


@pytest.mark.asyncio
async def test_normal_goodbye_plays_before_disconnect_and_completes_once() -> None:
    order = []
    state = SessionState(room_name="synthetic-room")

    class FakeSpeech:
        async def wait_for_playout(self):
            order.append("goodbye_played")

    class FakeSession:
        def say(self, message):
            assert message == GOODBYE
            order.append("goodbye_started")
            return FakeSpeech()

    async def disconnect():
        order.append("room_deleted")

    async def complete():
        state.end()
        order.append("persist_ai_segment")

    runner = CompletionRunner(complete)
    await finish_normal_call(FakeSession(), disconnect, runner)
    await runner.run()
    assert order == [
        "goodbye_started",
        "goodbye_played",
        "room_deleted",
        "persist_ai_segment",
    ]


@pytest.mark.asyncio
async def test_stalled_goodbye_still_disconnects(monkeypatch) -> None:
    import asyncio

    monkeypatch.setattr("agent.GOODBYE_PLAYOUT_TIMEOUT_SECONDS", 0.001)
    order = []

    class StalledSpeech:
        async def wait_for_playout(self):
            await asyncio.sleep(60)

    class FakeSession:
        def say(self, message):
            assert message == GOODBYE
            return StalledSpeech()

    async def disconnect():
        order.append("room_deleted")

    async def complete():
        order.append("persisted")

    await finish_normal_call(FakeSession(), disconnect, CompletionRunner(complete))
    assert order == ["room_deleted", "persisted"]


@pytest.mark.asyncio
async def test_end_call_tool_marks_normal_end_but_cannot_interrupt_handoff() -> None:
    state = SessionState(room_name="synthetic-room")
    state.accept_consent()
    calls = []

    async def disconnect():
        calls.append("disconnect")

    workflow = SimpleNamespace(state=state, events=[], tool_error_count=0)
    assistant = ClaimsAssistant(workflow, on_end_call=disconnect)
    assert "end_call" in {tool.info.name for tool in assistant.tools}
    result = await ClaimsAssistant.end_call._func(assistant, None)
    assert result == {"status": "ended"}
    assert calls == ["disconnect"]
    assert state.phase == CallPhase.ENDING
    assert workflow.events == [TrustedEvent.CALL_ENDED]

    waiting = SessionState(room_name="other-room")
    waiting.accept_consent()
    waiting.request_handoff()
    other_workflow = SimpleNamespace(state=waiting, events=[], tool_error_count=0)
    other_assistant = ClaimsAssistant(other_workflow, on_end_call=disconnect)
    blocked = await ClaimsAssistant.end_call._func(other_assistant, None)
    assert blocked == {"status": "temporarily_unavailable"}
    assert calls == ["disconnect"]
    assert waiting.phase == CallPhase.ACTIVE
    assert other_workflow.events == []


@pytest.mark.asyncio
async def test_end_call_failure_tells_caller_to_disconnect_manually() -> None:
    state = SessionState(room_name="synthetic-room")
    state.accept_consent()

    async def failed_disconnect():
        raise RuntimeError("synthetic deletion failure")

    workflow = SimpleNamespace(state=state, events=[], tool_error_count=0)
    assistant = ClaimsAssistant(workflow, on_end_call=failed_disconnect)
    result = await ClaimsAssistant.end_call._func(assistant, None)
    assert result["status"] == "disconnect_failed"
    assert "hang up from your side" in result["spoken_message"]
    assert workflow.tool_error_count == 1
