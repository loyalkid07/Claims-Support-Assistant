"""Testable conversation-to-domain boundary; no model-supplied authorization state."""

import re
from contextlib import suppress

from errors import PolicyError
from faq import FaqSnapshot
from handoff import (
    HANDOFF_REASON_CATEGORIES,
    HANDOFF_WAIT_SECONDS,
    SupabaseHandoffRepository,
)
from identifiers import extract_labeled_bundle
from postcall import FAQ_TOPIC_EVENTS, CallerCue, TrustedEvent
from session_state import Auth, Handoff, Route, SessionState
from supabase_claims import SupabaseClaimsRepository
from verification import IdentityVerifier


class VoiceWorkflow:
    def __init__(
        self,
        state: SessionState,
        verifier: IdentityVerifier,
        claims: SupabaseClaimsRepository,
        faq: FaqSnapshot,
        handoffs: SupabaseHandoffRepository | None = None,
    ) -> None:
        self.state = state
        self.verifier = verifier
        self.claims = claims
        self.faq = faq
        self.handoffs = handoffs
        self.events: list[TrustedEvent] = []
        self.caller_cues: set[CallerCue] = set()
        self.tool_error_count = 0
        self._confirmation_signal: bool | None = None

    def caller_turn_committed(self, text: str) -> None:
        """Capture a trusted caller turn and any clearly labeled claim/ZIP pair."""

        self.verifier.on_caller_turn()
        normalized = re.sub(r"\s+", " ", text.strip().lower())
        # Store only bounded labels, never the caller's raw verification factors.
        if re.search(
            r"\b(frustrated|upset|angry|ridiculous|you keep misunderstanding)\b",
            normalized,
        ):
            self.caller_cues.add(CallerCue.FRUSTRATED)
        if re.search(
            r"\b(thank you|thanks|that helps|that's helpful|relieved|appreciate it)\b",
            normalized,
        ):
            self.caller_cues.add(CallerCue.SATISFIED)
        if len(normalized.split()) >= 3:
            self.caller_cues.add(CallerCue.INFORMATIONAL)
        if re.match(
            r"^(yes|yeah|yep|sure|okay|ok|correct|right|exactly|"
            r"that's right|that is right|that's correct|that is correct)\b",
            normalized,
        ):
            self._confirmation_signal = True
        elif re.match(
            r"^(no|nope|incorrect|not quite|that's wrong|that is wrong)\b",
            normalized,
        ):
            self._confirmation_signal = False
        else:
            self._confirmation_signal = None
        bundle = extract_labeled_bundle(text)
        if bundle is not None:
            with suppress(PolicyError):
                self.prepare_bundle(*bundle)

    def prepare_phone(self, spoken: str) -> dict:
        self.state.require_identifier_access()
        self.state.change_route(Route.ACCOUNT_ACCESS)
        last_four = self.verifier.prepare_phone(spoken)
        self._confirmation_signal = None
        return {"status": "confirm_phone", "last_four": last_four}

    async def confirm_phone(self, confirmed: bool) -> dict:
        if confirmed is not self._confirmation_signal:
            raise PolicyError("CONFIRMATION_NOT_CONFIRMED")
        self._confirmation_signal = None
        return await self.verifier.confirm_phone(confirmed=confirmed)

    def prepare_bundle(self, claim_id_spoken: str, postal_spoken: str) -> dict:
        self.state.require_identifier_access()
        self.state.change_route(Route.ACCOUNT_ACCESS)
        self.verifier.prepare_bundle(claim_id_spoken, postal_spoken)
        return {"status": "confirm_bundle"}

    async def confirm_bundle(self, confirmed: bool) -> dict:
        if confirmed is not self._confirmation_signal:
            raise PolicyError("CONFIRMATION_NOT_CONFIRMED")
        self._confirmation_signal = None
        result = await self.verifier.confirm_bundle(confirmed=confirmed)
        if result["status"] in {"retry", "locked"}:
            self.events.append(TrustedEvent.VERIFICATION_FAILED)
            result["spoken_message"] = (
                "I could not verify an account with the information provided. "
                "We can recheck the phone number, claim number, and ZIP, or I can "
                "connect you with a representative."
                if result["status"] == "retry"
                else "I could not verify an account with the information provided. "
                "I can try to connect you with a representative if you'd like."
            )
        return result

    async def get_claim_status(self) -> dict:
        result = await self.claims.get_claim_status(self.state)
        self.events.append(TrustedEvent.CLAIM_STATUS_PROVIDED)
        return result

    def get_faq(self, topic_id: str) -> dict:
        result = self.faq.get_faq(topic_id)
        if result["status"] == "found":
            self.events.append(TrustedEvent.FAQ_ANSWERED)
            self.events.append(FAQ_TOPIC_EVENTS[topic_id])
        return result

    async def request_representative(self, reason_category: str | None = None) -> dict:
        """Start an expiring handoff without requiring identity or a reason."""

        if reason_category not in HANDOFF_REASON_CATEGORIES:
            reason_category = None
        self.state.request_handoff()
        self.events.append(TrustedEvent.HANDOFF_REQUESTED)
        if self.handoffs is None:
            return self._handoff_unavailable()
        caller_name = None
        if self.state.auth == Auth.VERIFIED:
            with suppress(PolicyError):
                caller_name = await self.claims.get_verified_caller_name(self.state)
        try:
            request = await self.handoffs.create_waiting(
                self.state, tuple(self.events), caller_name, reason_category
            )
        except PolicyError:
            return self._handoff_unavailable()
        self.state.wait_for_handoff(request.handoff_id, request.expires_at_utc)
        return {
            "status": "waiting",
            "handoff_id": request.handoff_id,
            "wait_seconds": HANDOFF_WAIT_SECONDS,
        }

    async def supervisor_joined(
        self, room_name: str, identity: str, attributes: dict[str, str]
    ) -> bool:
        """Only a matching room event and a conditional database write confirm success."""

        handoff_id = self.state.handoff_id
        if (
            self.state.handoff != Handoff.WAITING
            or self.handoffs is None
            or handoff_id is None
            or room_name != self.state.room_name
            or not identity.startswith("supervisor-")
            or attributes.get("role") != "supervisor"
            or attributes.get("handoff_id") != handoff_id
        ):
            return False
        if not await self.handoffs.mark_connected(handoff_id, identity):
            return False
        self.state.connect_handoff(handoff_id)
        self.events.append(TrustedEvent.HANDOFF_CONNECTED)
        return True

    async def expire_handoff(self, handoff_id: str) -> bool:
        if (
            self.state.handoff != Handoff.WAITING
            or self.state.handoff_id != handoff_id
            or self.handoffs is None
        ):
            return False
        try:
            await self.handoffs.mark_expired(handoff_id)
        except PolicyError:
            # The deadline still prevents token issuance; fail locally rather
            # than leaving the caller waiting in silence.
            self.tool_error_count += 1
        self._handoff_unavailable(handoff_id)
        return True

    def _handoff_unavailable(self, handoff_id: str | None = None) -> dict:
        self.state.fail_handoff(handoff_id)
        self.events.append(TrustedEvent.HANDOFF_UNAVAILABLE)
        return {
            "status": "unavailable",
            "message": "I'm sorry, but I couldn't connect a representative right now. "
            "You can call again during our service hours. I cannot promise a callback.",
        }
