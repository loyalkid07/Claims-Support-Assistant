"""Local-only operator broker for short-lived, one-room supervisor tokens."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx
from livekit import api


@dataclass(frozen=True)
class WaitingHandoff:
    handoff_id: str
    room_name: str
    expires_at: datetime
    summary: dict


class SupervisorBroker:
    def __init__(
        self,
        supabase_url: str,
        supabase_key: str,
        livekit_url: str,
        livekit_key: str,
        livekit_secret: str,
        client: httpx.Client,
    ) -> None:
        parsed = urlparse(supabase_url)
        livekit = urlparse(livekit_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not parsed.hostname.endswith(".supabase.co")
            or parsed.path not in {"", "/"}
            or not supabase_key.startswith("sb_secret_")
            or livekit.scheme != "wss"
            or not livekit.hostname
            or not livekit_key
            or not livekit_secret
        ):
            raise ValueError("invalid server-only broker configuration")
        self._endpoint = supabase_url.rstrip("/") + "/rest/v1/handoff_requests"
        self._supabase_key = supabase_key
        self._livekit_url = livekit_url
        self._livekit_key = livekit_key
        self._livekit_secret = livekit_secret
        self._client = client

    def _parse_waiting(self, row: dict) -> WaitingHandoff:
        try:
            handoff_id = str(UUID(row["handoff_id"]))
            room = row["room_name"]
            expires = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
            summary = row["summary_json"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid handoff row") from exc
        if (
            row.get("state") != "WAITING"
            or not isinstance(room, str)
            or not 1 <= len(room) <= 128
            or expires.tzinfo is None
            or not isinstance(summary, dict)
            or summary.get("call_id") is None
        ):
            raise ValueError("invalid handoff row")
        return WaitingHandoff(handoff_id, room, expires, summary)

    def list_waiting(self) -> list[WaitingHandoff]:
        response = self._client.get(
            self._endpoint,
            params={
                "select": "handoff_id,room_name,state,summary_json,expires_at",
                "state": "eq.WAITING",
                "supervisor_identity": "is.null",
                "expires_at": f"gt.{datetime.now(timezone.utc).isoformat()}",
                "order": "created_at.asc",
                "limit": "20",
            },
            headers={"apikey": self._supabase_key},
            timeout=3.0,
        )
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list):
            raise ValueError("invalid handoff response")
        return [self._parse_waiting(row) for row in rows]

    def issue_token(self, handoff_id: str) -> dict[str, str]:
        try:
            handoff_id = str(UUID(handoff_id))
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid handoff ID") from exc
        response = self._client.get(
            self._endpoint,
            params={
                "select": "handoff_id,room_name,state,summary_json,expires_at",
                "handoff_id": f"eq.{handoff_id}",
                "state": "eq.WAITING",
                "supervisor_identity": "is.null",
                "limit": "1",
            },
            headers={"apikey": self._supabase_key},
            timeout=3.0,
        )
        response.raise_for_status()
        rows = response.json()
        if not isinstance(rows, list) or len(rows) != 1:
            raise ValueError("handoff is not waiting")
        waiting = self._parse_waiting(rows[0])
        remaining = (waiting.expires_at - datetime.now(timezone.utc)).total_seconds()
        if remaining < 3:
            raise ValueError("handoff has expired")
        identity = f"supervisor-{uuid4().hex}"
        response = self._client.patch(
            self._endpoint,
            params={
                "handoff_id": f"eq.{handoff_id}",
                "state": "eq.WAITING",
                "supervisor_identity": "is.null",
                "expires_at": f"gt.{datetime.now(timezone.utc).isoformat()}",
            },
            headers={
                "apikey": self._supabase_key,
                "Prefer": "return=representation",
            },
            json={"supervisor_identity": identity},
            timeout=3.0,
        )
        response.raise_for_status()
        claimed = response.json()
        if (
            not isinstance(claimed, list)
            or len(claimed) != 1
            or claimed[0].get("handoff_id") != handoff_id
            or claimed[0].get("supervisor_identity") != identity
        ):
            raise ValueError("handoff was claimed or expired")
        ttl = timedelta(seconds=min(60, max(1, int(remaining))))
        token = (
            api.AccessToken(self._livekit_key, self._livekit_secret)
            .with_identity(identity)
            .with_name("Claims supervisor")
            .with_attributes({"role": "supervisor", "handoff_id": handoff_id})
            .with_grants(
                api.VideoGrants(
                    room_join=True,
                    room=waiting.room_name,
                    can_publish=True,
                    can_subscribe=True,
                    can_publish_data=False,
                    can_publish_sources=["microphone"],
                    can_update_own_metadata=False,
                )
            )
            .with_ttl(ttl)
            .to_jwt()
        )
        return {"url": self._livekit_url, "token": token}
