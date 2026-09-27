"""Deterministic, sanitized fallback for one AI-segment interaction."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum

from session_state import Auth, SessionState


class CallerCue(str, Enum):
    SATISFIED = "SATISFIED"
    FRUSTRATED = "FRUSTRATED"
    INFORMATIONAL = "INFORMATIONAL"


class TrustedEvent(str, Enum):
    FAQ_ANSWERED = "FAQ_ANSWERED"
    CLAIM_STATUS_PROVIDED = "CLAIM_STATUS_PROVIDED"
    VERIFICATION_FAILED = "VERIFICATION_FAILED"
    HANDOFF_REQUESTED = "HANDOFF_REQUESTED"
    HANDOFF_CONNECTED = "HANDOFF_CONNECTED"
    HANDOFF_UNAVAILABLE = "HANDOFF_UNAVAILABLE"
    CALLER_DISCONNECTED = "CALLER_DISCONNECTED"


CALLER_SENTIMENT_RUBRIC = (
    "Judge only the caller's turns in the AI segment. Agent politeness is not evidence. "
    "Use positive for clear caller satisfaction or relief, negative for clear caller "
    "frustration or upset, mixed when both occur, neutral for informational turns "
    "without strong emotion, and unknown when caller evidence is insufficient. "
    "A failed verification or repeated misunderstanding is context, not proof of "
    "the caller's emotion. Never infer sentiment from the agent's tone."
)


@dataclass(frozen=True)
class Interaction:
    call_id: str
    caller_name: str
    summary: str
    sentiment: str
    timestamp_utc: str
    outcome: str
    authenticated: bool
    handoff_reason: str | None = None
    tool_error_count: int = 0
    duration_seconds: int = 0
    agent_version: str = "local-dev"
    prompt_version: str = "local-dev"
    workflow_version: str = "local-dev"
    kb_version: str = "faq-v1"
    model_stack_version: str = "local-dev"
    trace_id: str = ""


class CompletionOnce:
    """Cache the first AI-segment record; the database later enforces call_id uniqueness."""

    def __init__(self) -> None:
        self._interaction: Interaction | None = None

    def build(
        self,
        state: SessionState,
        events: tuple[TrustedEvent, ...],
        caller_cues: tuple[CallerCue, ...] = (),
        *,
        verified_name: str | None = None,
    ) -> Interaction:
        if self._interaction is None:
            self._interaction = build_fallback_interaction(
                state, events, caller_cues, verified_name=verified_name
            )
        elif self._interaction.call_id != state.call_id:
            raise ValueError("completion belongs to a different call")
        return self._interaction


class CompletionRunner:
    """Share one durable completion task across handoff and job shutdown."""

    def __init__(self, complete: Callable[[], Awaitable[None]]) -> None:
        self._complete = complete
        self._task: asyncio.Task[None] | None = None

    async def run(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._complete())
        try:
            await asyncio.shield(self._task)
        except asyncio.CancelledError:
            # The job-shutdown hook can still await the same in-flight task.
            await asyncio.shield(self._task)


def caller_sentiment(cues: tuple[CallerCue, ...]) -> str:
    positive = CallerCue.SATISFIED in cues
    negative = CallerCue.FRUSTRATED in cues
    if positive and negative:
        return "mixed"
    if negative:
        return "negative"
    if positive:
        return "positive"
    if CallerCue.INFORMATIONAL in cues:
        return "neutral"
    return "unknown"


def build_fallback_interaction(
    state: SessionState,
    events: tuple[TrustedEvent, ...],
    caller_cues: tuple[CallerCue, ...] = (),
    *,
    verified_name: str | None = None,
) -> Interaction:
    """Build repeatable content from event codes, never from raw transcript text."""

    if state.ended_at_utc is None:
        raise ValueError("call must be ended before completion is built")
    authenticated = state.auth == Auth.VERIFIED
    caller_name = (
        verified_name if authenticated and verified_name else "unknown/unverified"
    )
    observed = set(events)
    if TrustedEvent.HANDOFF_CONNECTED in observed:
        outcome = "handed_off"
    elif TrustedEvent.HANDOFF_UNAVAILABLE in observed:
        outcome = "handoff_unavailable"
    elif TrustedEvent.CLAIM_STATUS_PROVIDED in observed and authenticated:
        outcome = "claim_status_provided"
    elif TrustedEvent.VERIFICATION_FAILED in observed:
        outcome = "verification_unsuccessful"
    elif TrustedEvent.FAQ_ANSWERED in observed:
        outcome = "faq_resolved"
    else:
        outcome = (
            "caller_disconnected"
            if TrustedEvent.CALLER_DISCONNECTED in observed
            else "ended"
        )
    claim_done = TrustedEvent.CLAIM_STATUS_PROVIDED in observed and authenticated
    faq_done = TrustedEvent.FAQ_ANSWERED in observed
    verification_failed = TrustedEvent.VERIFICATION_FAILED in observed
    if claim_done and faq_done:
        summary = "The verified caller received a claim status update and approved general claims guidance."
    elif claim_done:
        summary = "The verified caller received a claim status update."
    elif verification_failed and faq_done:
        summary = "The caller could not complete verification but received approved general claims guidance."
    elif verification_failed:
        summary = (
            "The caller requested claim information, but verification did not complete."
        )
    elif faq_done:
        summary = "The caller received approved general claims guidance."
    elif TrustedEvent.HANDOFF_REQUESTED in observed:
        summary = "The caller requested a representative."
    else:
        summary = "The AI interaction ended without a completed claim-status request."
    if TrustedEvent.HANDOFF_REQUESTED in observed and "representative" not in summary:
        summary += " The caller requested a representative."
    if TrustedEvent.HANDOFF_CONNECTED in observed:
        summary += " A supervisor joined the AI segment."
    elif TrustedEvent.HANDOFF_UNAVAILABLE in observed:
        summary += " No supervisor joined the AI segment."
    elif TrustedEvent.HANDOFF_REQUESTED in observed:
        summary += " The handoff outcome was not confirmed."
    return Interaction(
        call_id=state.call_id,
        caller_name=caller_name,
        summary=summary,
        sentiment=caller_sentiment(caller_cues),
        timestamp_utc=state.ended_at_utc.isoformat(),
        outcome=outcome,
        authenticated=authenticated,
        duration_seconds=max(
            0, int((state.ended_at_utc - state.started_at_utc).total_seconds())
        ),
    )
