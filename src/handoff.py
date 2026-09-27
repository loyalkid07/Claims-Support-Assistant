"""Sanitized, expiring same-room supervisor handoff requests."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx

from errors import PolicyError
from postcall import TrustedEvent
from session_state import Auth, SessionState

HANDOFF_WAIT_SECONDS = 60


@dataclass(frozen=True)
class HandoffRequest:
    handoff_id: str
    room_name: str
    expires_at_utc: datetime


def minimum_handoff_summary(
    state: SessionState, events: tuple[TrustedEvent, ...], caller_name: str | None
) -> dict:
    """Send only bounded, trusted AI-segment facts to an authorized supervisor."""

    observed = set(events)
    actions = []
    if TrustedEvent.FAQ_ANSWERED in observed:
        actions.append("public FAQ answered")
    if TrustedEvent.CLAIM_STATUS_PROVIDED in observed and state.auth == Auth.VERIFIED:
        actions.append("claim status provided")
    if TrustedEvent.VERIFICATION_FAILED in observed:
        actions.append("verification unsuccessful")
    return {
        "call_id": state.call_id,
        "verification_state": (
            "VERIFIED"
            if state.auth == Auth.VERIFIED
            else "LOCKED"
            if state.auth == Auth.LOCKED
            else "UNVERIFIED"
        ),
        "caller_name": (
            caller_name
            if state.auth == Auth.VERIFIED and caller_name
            else "unknown/unverified"
        ),
        "caller_requested_reason": None,
        "completed_actions": actions,
        "open_need": "caller requested a representative",
        "safety_flag": "NONE",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }


class SupabaseHandoffRepository:
    """Service-key-only request state; never expose this client to the browser."""

    def __init__(self, url: str, secret_key: str, client: httpx.AsyncClient) -> None:
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not parsed.hostname.endswith(".supabase.co")
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or not secret_key.startswith("sb_secret_")
        ):
            raise ValueError("invalid Supabase endpoint or secret key")
        self._endpoint = url.rstrip("/") + "/rest/v1/handoff_requests"
        self._key = secret_key
        self._client = client

    async def create_waiting(
        self,
        state: SessionState,
        events: tuple[TrustedEvent, ...],
        caller_name: str | None,
    ) -> HandoffRequest:
        handoff_id = str(uuid4())
        expires = datetime.now(timezone.utc) + timedelta(seconds=HANDOFF_WAIT_SECONDS)
        row = {
            "handoff_id": handoff_id,
            "call_id": state.call_id,
            "room_name": state.room_name,
            "state": "WAITING",
            "summary_json": minimum_handoff_summary(state, events, caller_name),
            "expected_role": "supervisor",
            "expires_at": expires.isoformat(),
        }
        try:
            response = await self._client.post(
                self._endpoint,
                headers={"apikey": self._key, "Prefer": "return=representation"},
                json=row,
                timeout=3.0,
            )
        except httpx.RequestError as exc:
            raise PolicyError("UPSTREAM_UNAVAILABLE") from exc
        if response.status_code != 201:
            raise PolicyError("UPSTREAM_UNAVAILABLE")
        try:
            rows = response.json()
        except ValueError as exc:
            raise PolicyError("VALIDATION_ERROR") from exc
        if (
            not isinstance(rows, list)
            or len(rows) != 1
            or rows[0].get("handoff_id") != handoff_id
        ):
            raise PolicyError("VALIDATION_ERROR")
        return HandoffRequest(handoff_id, state.room_name, expires)

    async def mark_connected(self, handoff_id: str, identity: str) -> bool:
        UUID(handoff_id)
        if not identity.startswith("supervisor-") or len(identity) > 80:
            return False
        try:
            response = await self._client.patch(
                self._endpoint,
                params={
                    "handoff_id": f"eq.{handoff_id}",
                    "state": "eq.WAITING",
                    "supervisor_identity": f"eq.{identity}",
                    "expires_at": f"gt.{datetime.now(timezone.utc).isoformat()}",
                },
                headers={"apikey": self._key, "Prefer": "return=representation"},
                json={
                    "state": "CONNECTED",
                    "connected_at": datetime.now(timezone.utc).isoformat(),
                },
                timeout=3.0,
            )
        except httpx.RequestError as exc:
            raise PolicyError("UPSTREAM_UNAVAILABLE") from exc
        if response.status_code != 200:
            raise PolicyError("UPSTREAM_UNAVAILABLE")
        rows = response.json()
        return isinstance(rows, list) and len(rows) == 1

    async def mark_expired(self, handoff_id: str) -> bool:
        UUID(handoff_id)
        try:
            response = await self._client.patch(
                self._endpoint,
                params={"handoff_id": f"eq.{handoff_id}", "state": "eq.WAITING"},
                headers={"apikey": self._key, "Prefer": "return=representation"},
                json={"state": "EXPIRED"},
                timeout=3.0,
            )
        except httpx.RequestError as exc:
            raise PolicyError("UPSTREAM_UNAVAILABLE") from exc
        if response.status_code != 200:
            raise PolicyError("UPSTREAM_UNAVAILABLE")
        rows = response.json()
        return isinstance(rows, list) and len(rows) == 1

    async def mark_cancelled(self, handoff_id: str) -> bool:
        UUID(handoff_id)
        try:
            response = await self._client.patch(
                self._endpoint,
                params={"handoff_id": f"eq.{handoff_id}", "state": "eq.WAITING"},
                headers={"apikey": self._key, "Prefer": "return=representation"},
                json={"state": "CANCELLED"},
                timeout=3.0,
            )
        except httpx.RequestError as exc:
            raise PolicyError("UPSTREAM_UNAVAILABLE") from exc
        if response.status_code != 200:
            raise PolicyError("UPSTREAM_UNAVAILABLE")
        rows = response.json()
        return isinstance(rows, list) and len(rows) == 1
