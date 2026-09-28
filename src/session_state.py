"""Server-owned, per-call state transitions for the claims demo."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from errors import PolicyError


class Consent(str, Enum):
    PENDING = "PENDING"
    ACCEPTED = "ACCEPTED"
    DECLINED = "DECLINED"


class CallPhase(str, Enum):
    ACTIVE = "ACTIVE"
    ENDING = "ENDING"
    ENDED = "ENDED"


class Auth(str, Enum):
    UNVERIFIED = "UNVERIFIED"
    CANDIDATE_READY = "CANDIDATE_READY"
    VERIFIED = "VERIFIED"
    LOCKED = "LOCKED"


class Route(str, Enum):
    GENERAL = "GENERAL"
    ACCOUNT_ACCESS = "ACCOUNT_ACCESS"
    FAQ = "FAQ"
    HANDOFF = "HANDOFF"
    SAFETY = "SAFETY"


class Handoff(str, Enum):
    NONE = "NONE"
    REQUESTED = "REQUESTED"
    WAITING = "WAITING"
    CONNECTED = "CONNECTED"
    FAILED = "FAILED"


@dataclass
class SessionState:
    room_name: str
    call_id: str = field(default_factory=lambda: str(uuid4()))
    started_at_utc: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ended_at_utc: datetime | None = None
    consent: Consent = Consent.PENDING
    phase: CallPhase = CallPhase.ACTIVE
    auth: Auth = Auth.UNVERIFIED
    route: Route = Route.GENERAL
    attempts_used: int = 0
    request_generation: int = 0
    candidate_ref: str | None = None
    verified_customer_ref: str | None = None
    verified_claim_ref: str | None = None
    handoff: Handoff = Handoff.NONE
    handoff_id: str | None = None
    handoff_expires_at_utc: datetime | None = None

    def accept_consent(self) -> None:
        if self.phase != CallPhase.ACTIVE or self.consent != Consent.PENDING:
            raise PolicyError("INVALID_TRANSITION")
        self.consent = Consent.ACCEPTED

    def decline_consent(self) -> None:
        if self.phase != CallPhase.ACTIVE or self.consent != Consent.PENDING:
            raise PolicyError("INVALID_TRANSITION")
        self.consent = Consent.DECLINED
        self.phase = CallPhase.ENDING

    def require_identifier_access(self) -> None:
        if self.consent != Consent.ACCEPTED:
            raise PolicyError("CONSENT_REQUIRED")
        if self.phase != CallPhase.ACTIVE:
            raise PolicyError("INVALID_TRANSITION")
        if self.auth == Auth.LOCKED:
            raise PolicyError("AUTH_LOCKED")
        if self.auth == Auth.VERIFIED:
            raise PolicyError("INVALID_TRANSITION")

    def new_lookup_generation(self) -> int:
        self.require_identifier_access()
        self.request_generation += 1
        self.candidate_ref = None
        self.auth = Auth.UNVERIFIED
        return self.request_generation

    def set_candidate(self, generation: int, candidate_ref: str) -> None:
        self.require_identifier_access()
        if generation != self.request_generation:
            raise PolicyError("STALE_REQUEST")
        self.candidate_ref = candidate_ref
        self.auth = Auth.CANDIDATE_READY

    def reject_stale(self, generation: int) -> None:
        if generation != self.request_generation or self.phase != CallPhase.ACTIVE:
            raise PolicyError("STALE_REQUEST")

    def verify(self, customer_ref: str, claim_ref: str) -> None:
        self.require_identifier_access()
        if self.auth != Auth.CANDIDATE_READY or not self.candidate_ref:
            raise PolicyError("INVALID_TRANSITION")
        self.attempts_used += 1
        self.auth = Auth.VERIFIED
        self.verified_customer_ref = customer_ref
        self.verified_claim_ref = claim_ref

    def verification_failed(self) -> int:
        self.require_identifier_access()
        if self.auth != Auth.CANDIDATE_READY or not self.candidate_ref:
            raise PolicyError("INVALID_TRANSITION")
        self.attempts_used += 1
        if self.attempts_used >= 3:
            self.auth = Auth.LOCKED
        return 3 - self.attempts_used

    def require_claim_ref(self) -> str:
        if self.auth != Auth.VERIFIED or not self.verified_claim_ref:
            raise PolicyError("UNAUTHENTICATED")
        if self.phase != CallPhase.ACTIVE:
            raise PolicyError("INVALID_TRANSITION")
        return self.verified_claim_ref

    def model_view(self) -> dict[str, str | int]:
        """Expose workflow state without candidate kind, refs, or factors."""

        return {
            "consent": self.consent.value.lower(),
            "verification": self.auth.value.lower(),
            "attempts_remaining": max(0, 3 - self.attempts_used),
            "route": self.route.value.lower(),
        }

    def change_route(self, route: Route) -> None:
        if self.phase != CallPhase.ACTIVE:
            raise PolicyError("INVALID_TRANSITION")
        if route != self.route:
            self.request_generation += 1
            self.route = route

    def request_handoff(self) -> None:
        if self.consent != Consent.ACCEPTED or self.phase != CallPhase.ACTIVE:
            raise PolicyError("CONSENT_REQUIRED")
        if self.handoff not in {Handoff.NONE, Handoff.FAILED}:
            raise PolicyError("HANDOFF_IN_PROGRESS")
        self.change_route(Route.HANDOFF)
        self.handoff = Handoff.REQUESTED
        self.handoff_id = None
        self.handoff_expires_at_utc = None

    def wait_for_handoff(self, handoff_id: str, expires_at_utc: datetime) -> None:
        if self.handoff != Handoff.REQUESTED or self.phase != CallPhase.ACTIVE:
            raise PolicyError("INVALID_TRANSITION")
        self.handoff_id = handoff_id
        self.handoff_expires_at_utc = expires_at_utc
        self.handoff = Handoff.WAITING

    def connect_handoff(self, handoff_id: str) -> None:
        if self.handoff != Handoff.WAITING or self.handoff_id != handoff_id:
            raise PolicyError("INVALID_TRANSITION")
        self.handoff = Handoff.CONNECTED

    def fail_handoff(self, handoff_id: str | None = None) -> None:
        if self.handoff not in {Handoff.REQUESTED, Handoff.WAITING}:
            raise PolicyError("INVALID_TRANSITION")
        if handoff_id is not None and self.handoff_id != handoff_id:
            raise PolicyError("INVALID_TRANSITION")
        self.handoff = Handoff.FAILED

    def end(self) -> None:
        if self.phase != CallPhase.ENDED:
            self.phase = CallPhase.ENDED
            self.ended_at_utc = datetime.now(timezone.utc)

    def begin_ending(self) -> None:
        """Allow a normal hangup only while the AI still owns the call."""

        if self.phase != CallPhase.ACTIVE or self.handoff in {
            Handoff.REQUESTED,
            Handoff.WAITING,
            Handoff.CONNECTED,
        }:
            raise PolicyError("INVALID_TRANSITION")
        self.phase = CallPhase.ENDING
