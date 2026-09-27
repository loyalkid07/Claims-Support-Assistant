"""Idempotent Airtable delivery for sanitized, already-durable interactions."""

import re
from uuid import UUID

import httpx

from postcall import Interaction


class DeliveryError(Exception):
    """Controlled delivery code; never include response bodies or credentials."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def airtable_fields(interaction: Interaction) -> dict:
    return {
        "Call ID": interaction.call_id,
        "Caller Name": interaction.caller_name,
        "Summary": interaction.summary,
        "Sentiment": interaction.sentiment,
        "Timestamp UTC": interaction.timestamp_utc,
        "Outcome": interaction.outcome,
        "Authenticated": interaction.authenticated,
        "Handoff Reason": interaction.handoff_reason,
        "Tool Error Count": interaction.tool_error_count,
        "Duration Seconds": interaction.duration_seconds,
        "Agent Version": interaction.agent_version,
        "Prompt Version": interaction.prompt_version,
        "Workflow Version": interaction.workflow_version,
        "KB Version": interaction.kb_version,
        "Model Stack": interaction.model_stack_version,
        "Trace ID": interaction.trace_id,
    }


class AirtableWriter:
    def __init__(
        self, pat: str, base_id: str, table_id: str, client: httpx.AsyncClient
    ) -> None:
        if (
            not pat.startswith("pat")
            or not re.fullmatch(r"app[A-Za-z0-9]+", base_id)
            or not re.fullmatch(r"tbl[A-Za-z0-9]+", table_id)
        ):
            raise ValueError("invalid Airtable configuration")
        self._url = f"https://api.airtable.com/v0/{base_id}/{table_id}"
        self._headers = {"Authorization": f"Bearer {pat}"}
        self._client = client

    async def _request(self, method: str, **kwargs) -> httpx.Response:
        try:
            response = await self._client.request(
                method,
                self._url,
                headers=self._headers,
                timeout=5.0,
                **kwargs,
            )
        except httpx.RequestError as exc:
            # A request can reach Airtable before its response is lost.
            raise DeliveryError("UNCERTAIN") from exc
        if response.status_code in {401, 403}:
            raise DeliveryError("PERMISSION_DENIED")
        if response.status_code == 429:
            raise DeliveryError("RATE_LIMITED")
        if response.status_code in {400, 404, 422}:
            raise DeliveryError("VALIDATION_ERROR")
        if response.status_code >= 500:
            raise DeliveryError("UPSTREAM_UNAVAILABLE")
        if response.status_code >= 400:
            raise DeliveryError("UPSTREAM_UNAVAILABLE")
        return response

    async def find_existing(self, call_id: str) -> str | None:
        UUID(call_id)  # Formula input is an internally generated UUID.
        response = await self._request(
            "GET",
            params={
                "filterByFormula": f'{{Call ID}}="{call_id}"',
                "maxRecords": "2",
                "fields[]": "Call ID",
            },
        )
        try:
            records = response.json()["records"]
            if not isinstance(records, list):
                raise ValueError
            matches = [
                record["id"]
                for record in records
                if record["fields"].get("Call ID") == call_id
            ]
            if any(not isinstance(record_id, str) for record_id in matches):
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise DeliveryError("VALIDATION_ERROR") from exc
        if len(matches) > 1:
            raise DeliveryError("CONFLICT")
        return matches[0] if matches else None

    async def deliver(
        self, interaction: Interaction, *, reconcile_first: bool = False
    ) -> str:
        if reconcile_first:
            existing = await self.find_existing(interaction.call_id)
            if existing is not None:
                return existing
        response = await self._request(
            "PATCH",
            json={
                "performUpsert": {"fieldsToMergeOn": ["Call ID"]},
                "records": [{"fields": airtable_fields(interaction)}],
            },
        )
        try:
            records = response.json()["records"]
            if len(records) != 1 or not isinstance(records[0]["id"], str):
                raise ValueError
            return records[0]["id"]
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise DeliveryError("VALIDATION_ERROR") from exc
