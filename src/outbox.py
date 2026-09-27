"""Durable one-row-per-call queue followed by one immediate Airtable attempt."""

import hashlib
import json
import re
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from uuid import UUID

import httpx

from airtable_writer import AirtableWriter, DeliveryError
from errors import PolicyError
from postcall import Interaction


def _payload(interaction: Interaction) -> tuple[dict, str]:
    UUID(interaction.call_id)
    data = asdict(interaction)
    summary = interaction.summary
    if (
        len(summary) > 2000
        or re.search(r"\bCLM[- ]?\d{6}\b", summary, re.I)
        or re.search(r"\b\d{10,11}\b", summary)
        or re.search(r"\b\d{5}(?:-\d{4})?\b", summary)
    ):
        raise PolicyError("VALIDATION_ERROR")
    serialized = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return data, hashlib.sha256(serialized.encode()).hexdigest()


class SupabaseOutbox:
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
            raise ValueError("invalid Supabase outbox configuration")
        self._url = url.rstrip("/") + "/rest/v1/interaction_outbox"
        self._headers = {"apikey": secret_key}
        self._client = client

    async def _request(self, method: str, **kwargs) -> httpx.Response:
        extra_headers = kwargs.pop("headers", {})
        try:
            response = await self._client.request(
                method,
                self._url,
                headers={**self._headers, **extra_headers},
                timeout=5.0,
                **kwargs,
            )
        except httpx.TimeoutException as exc:
            raise PolicyError("UPSTREAM_TIMEOUT") from exc
        except httpx.RequestError as exc:
            raise PolicyError("UPSTREAM_UNAVAILABLE") from exc
        if response.status_code in {401, 403}:
            raise PolicyError("PERMISSION_DENIED")
        if response.status_code == 429:
            raise PolicyError("RATE_LIMITED")
        if response.status_code >= 500:
            raise PolicyError("UPSTREAM_UNAVAILABLE")
        return response

    async def enqueue(self, interaction: Interaction) -> bool:
        """Return True only for a newly inserted row; conflict must match its hash."""

        payload, digest = _payload(interaction)
        response = await self._request(
            "POST",
            json={
                "call_id": interaction.call_id,
                "payload": payload,
                "payload_hash": digest,
            },
            headers={"Prefer": "return=representation"},
        )
        if response.status_code == 409:
            existing = await self._request(
                "GET",
                params={
                    "select": "payload_hash",
                    "call_id": f"eq.{interaction.call_id}",
                    "limit": "1",
                },
            )
            try:
                rows = existing.json()
                if len(rows) != 1 or rows[0]["payload_hash"] != digest:
                    raise ValueError
            except (KeyError, TypeError, ValueError) as exc:
                raise PolicyError("CONFLICT") from exc
            return False
        if response.status_code not in {200, 201}:
            raise PolicyError("UPSTREAM_UNAVAILABLE")
        return True

    async def mark(
        self,
        call_id: str,
        state: str,
        *,
        record_id: str | None = None,
        error_code: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc)
        update: dict[str, object] = {
            "delivery_state": state,
            "attempt_count": 1,
            "updated_at": now.isoformat(),
            "last_error_code": error_code,
            "last_error_at": now.isoformat() if error_code else None,
            "next_attempt_at": (
                (now + timedelta(seconds=5)).isoformat()
                if state in {"RETRY_PENDING", "UNCERTAIN"}
                else None
            ),
            "delivered_at": now.isoformat() if state == "DELIVERED" else None,
        }
        if record_id is not None:
            update["airtable_record_id"] = record_id
        response = await self._request(
            "PATCH",
            params={"call_id": f"eq.{call_id}", "delivery_state": "eq.QUEUED"},
            json=update,
            headers={"Prefer": "return=representation"},
        )
        if response.status_code not in {200, 204}:
            raise PolicyError("UPSTREAM_UNAVAILABLE")
        try:
            rows = response.json()
            if len(rows) != 1:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise PolicyError("CONFLICT") from exc


class InteractionDelivery:
    def __init__(self, outbox: SupabaseOutbox, airtable: AirtableWriter) -> None:
        self._outbox = outbox
        self._airtable = airtable

    async def queue_and_attempt(self, interaction: Interaction) -> str:
        """Persist first. A separate retry worker is deliberately not claimed here."""

        created = await self._outbox.enqueue(interaction)
        if not created:
            return "already_queued"
        try:
            record_id = await self._airtable.deliver(interaction)
        except DeliveryError as exc:
            state = {
                "UNCERTAIN": "UNCERTAIN",
                "PERMISSION_DENIED": "DEAD_LETTER",
                "VALIDATION_ERROR": "DEAD_LETTER",
                "CONFLICT": "DEAD_LETTER",
            }.get(exc.code, "RETRY_PENDING")
            await self._outbox.mark(interaction.call_id, state, error_code=exc.code)
            return state.lower()
        await self._outbox.mark(interaction.call_id, "DELIVERED", record_id=record_id)
        return "delivered"
