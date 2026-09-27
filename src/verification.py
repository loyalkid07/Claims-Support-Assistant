"""Deterministic verification with identical outward no-match and mismatch paths."""

import hmac
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from errors import PolicyError
from identifiers import (
    lookup_token,
    normalize_claim_id,
    normalize_phone,
    normalize_postal,
)
from session_state import Auth, SessionState


@dataclass(frozen=True)
class CustomerCandidate:
    """Restricted internal lookup result. Never return this to the model."""

    customer_ref: str
    postal_lookup_hmac: str


class CandidateRepository(Protocol):
    async def find_by_phone_token(self, token: str) -> CustomerCandidate | None: ...

    async def find_claim_for_candidate(
        self, customer_ref: str, claim_token: str
    ) -> str | None: ...


@dataclass
class _PendingPhone:
    token: str
    last_four: str
    generation: int
    ready_for_confirmation: bool = False


@dataclass
class _PendingBundle:
    claim_token: str
    postal_token: str
    ready_for_confirmation: bool = False


class IdentityVerifier:
    """One instance per call; raw factors are normalized and discarded promptly."""

    def __init__(
        self, state: SessionState, repository: CandidateRepository, hmac_secret: bytes
    ) -> None:
        if len(hmac_secret) < 32:
            raise ValueError("identifier HMAC secret must have at least 32 bytes")
        self.state = state
        self.repository = repository
        self._secret = hmac_secret
        self._pending_phone: _PendingPhone | None = None
        self._pending_bundle: _PendingBundle | None = None
        self._candidate: CustomerCandidate | None = None
        self._caller_turns = 0
        self._phone_confirmation_turn: int | None = None
        self.last_failure_reason: str | None = None

    def on_caller_turn(self) -> None:
        """Trusted turn boundary; model tools cannot supply or forge a turn index."""

        self._caller_turns += 1
        if self._pending_phone is not None:
            self._pending_phone.ready_for_confirmation = True
        if self._pending_bundle is not None:
            self._pending_bundle.ready_for_confirmation = True

    def prepare_phone(self, spoken: str) -> str:
        """Return only last four digits for confirmation; lookup has not run yet."""

        self.state.require_identifier_access()
        normalized = normalize_phone(spoken)
        generation = self.state.new_lookup_generation()
        self._candidate = None
        self._phone_confirmation_turn = None
        self._pending_phone = _PendingPhone(
            token=lookup_token(self._secret, "phone", normalized),
            last_four=normalized[-4:],
            generation=generation,
        )
        return self._pending_phone.last_four

    async def confirm_phone(self, *, confirmed: bool) -> dict:
        """Require a later caller turn; return no existence signal."""

        self.state.require_identifier_access()
        pending = self._pending_phone
        if pending is None or not pending.ready_for_confirmation:
            raise PolicyError("CONFIRMATION_REQUIRED")
        self._pending_phone = None
        if not confirmed:
            return {"status": "correction_needed"}
        self.state.reject_stale(pending.generation)
        candidate = await self.repository.find_by_phone_token(pending.token)
        self.state.reject_stale(pending.generation)
        self._candidate = candidate
        self.state.set_candidate(pending.generation, str(uuid4()))
        self._phone_confirmation_turn = self._caller_turns
        return {"status": "continue_verification"}

    def prepare_bundle(
        self, claim_id_spoken: str, postal_spoken: str
    ) -> tuple[str, str]:
        """Prepare complete factors, allowing both to be supplied before phone lookup."""

        self.state.require_identifier_access()
        claim_id = normalize_claim_id(claim_id_spoken)
        postal = normalize_postal(postal_spoken)
        self._pending_bundle = _PendingBundle(
            claim_token=lookup_token(self._secret, "claim", claim_id),
            postal_token=lookup_token(self._secret, "postal", postal),
        )
        return claim_id, postal

    async def confirm_bundle(self, *, confirmed: bool) -> dict:
        """Compare one confirmed complete bundle; only this can consume an attempt."""

        self.state.require_identifier_access()
        pending = self._pending_bundle
        if pending is None or not pending.ready_for_confirmation:
            raise PolicyError("CONFIRMATION_REQUIRED")
        if not confirmed:
            self._pending_bundle = None
            return {"status": "correction_needed"}
        if self.state.auth != Auth.CANDIDATE_READY:
            raise PolicyError("CANDIDATE_REQUIRED")
        if (
            self._phone_confirmation_turn is None
            or self._caller_turns <= self._phone_confirmation_turn
        ):
            raise PolicyError("CONFIRMATION_REQUIRED")
        self._pending_bundle = None

        generation = self.state.request_generation
        candidate = self._candidate
        # A placeholder lookup keeps the network path similar for unknown phones.
        customer_ref = candidate.customer_ref if candidate else str(uuid4())
        claim_ref = await self.repository.find_claim_for_candidate(
            customer_ref, pending.claim_token
        )
        self.state.reject_stale(generation)
        postal_matches = hmac.compare_digest(
            pending.postal_token,
            candidate.postal_lookup_hmac if candidate else "0" * 64,
        )
        if candidate and claim_ref and postal_matches:
            self.state.verify(candidate.customer_ref, claim_ref)
            self.last_failure_reason = None
            return {"status": "verified"}
        self.last_failure_reason = (
            "FACTORS_MISMATCH" if candidate else "CUSTOMER_NOT_FOUND"
        )

        remaining = self.state.verification_failed()
        if remaining:
            return {
                "status": "retry",
                "attempts_remaining": remaining,
                "message_key": "verification_generic_retry",
            }
        return {
            "status": "locked",
            "attempts_remaining": 0,
            "message_key": "verification_generic_final",
        }

    def authorized_claim_ref(self) -> str:
        """The claim adapter calls this and takes no model-supplied claim reference."""

        return self.state.require_claim_ref()
