"""Server-only Supabase reads and a strictly bounded spoken claim projection."""

import re
from datetime import date, datetime, timezone
from urllib.parse import urlparse
from uuid import UUID

import httpx

from errors import PolicyError
from faq import KB_VERSION, FaqSnapshot
from session_state import Auth, SessionState
from verification import CustomerCandidate

_CLAIM_FIELDS = (
    "claim_id_display,claim_type,loss_date,status_code,status_display,"
    "last_updated_at,next_action,estimated_next_step_at,"
    "required_documents,submission_method,submission_instructions,"
    "mailing_fallback_allowed,document_receipt_status,document_received_at,"
    "assigned_representative_name,assigned_representative_contact,record_version"
)
_STATUS_CODES = {"AWAITING_DOCUMENTS", "IN_REVIEW", "DOCUMENTS_RECEIVED", "CLOSED"}
_SUBMISSION_METHODS = {"SECURE_UPLOAD_ALREADY_ISSUED", "MAIL", "NONE", "UNKNOWN"}
_RECEIPT_STATUSES = {"RECEIVED", "PARTIAL", "NOT_CONFIRMED"}


def _utc_date(value: object) -> str:
    if not isinstance(value, str):
        raise PolicyError("VALIDATION_ERROR")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PolicyError("VALIDATION_ERROR") from exc
    if stamp.tzinfo is None:
        raise PolicyError("VALIDATION_ERROR")
    return stamp.astimezone(timezone.utc).date().isoformat()


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 500 or "http" in value.lower():
        raise PolicyError("VALIDATION_ERROR")
    return value


def _optional_date(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise PolicyError("VALIDATION_ERROR")
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise PolicyError("VALIDATION_ERROR") from exc


def _optional_utc_date(value: object) -> str | None:
    return None if value is None else _utc_date(value)


def claim_projection(row: dict) -> dict:
    """Whitelist and validate every field before returning anything to the agent."""

    try:
        claim_id = row["claim_id_display"]
        status_code = row["status_code"]
        status = row["status_display"]
        method = row["submission_method"]
        receipt = row["document_receipt_status"]
        documents = row["required_documents"]
        version = row["record_version"]
        mailing = row["mailing_fallback_allowed"]
        instructions = row["submission_instructions"]
    except KeyError as exc:
        raise PolicyError("VALIDATION_ERROR") from exc
    if (
        not isinstance(claim_id, str)
        or not re.fullmatch(r"CLM-[0-9]{6}", claim_id)
        or not isinstance(status_code, str)
        or status_code not in _STATUS_CODES
        or not isinstance(status, str)
        or not status
        or len(status) > 100
        or "http" in status.lower()
        or not isinstance(method, str)
        or method not in _SUBMISSION_METHODS
        or not isinstance(receipt, str)
        or receipt not in _RECEIPT_STATUSES
        or not isinstance(documents, list)
        or any(
            not isinstance(doc, str)
            or not doc
            or len(doc) > 100
            or "http" in doc.lower()
            for doc in documents
        )
        or type(version) is not int
        or version < 1
        or type(mailing) is not bool
        or not isinstance(instructions, str)
        or len(instructions) > 500
        or "http" in instructions.lower()
    ):
        raise PolicyError("VALIDATION_ERROR")
    received = row.get("document_received_at")
    if received is not None:
        _utc_date(received)
    claim_type = row.get("claim_type")
    if claim_type is not None and claim_type != "AUTO_PHYSICAL_DAMAGE":
        raise PolicyError("VALIDATION_ERROR")
    loss_date = _optional_date(row.get("loss_date"))
    last_updated_date = _utc_date(row.get("last_updated_at"))
    if loss_date is not None and loss_date > last_updated_date:
        raise PolicyError("VALIDATION_ERROR")
    estimated_next_step_date = _optional_utc_date(row.get("estimated_next_step_at"))
    rep_name = _optional_text(row.get("assigned_representative_name"))
    rep_contact = _optional_text(row.get("assigned_representative_contact"))
    return {
        "claim_id_display": claim_id,
        "claim_type": claim_type,
        "loss_date": loss_date,
        "status": status,
        "last_updated_date": last_updated_date,
        "next_action": _optional_text(row.get("next_action")),
        "estimated_next_step_date": estimated_next_step_date,
        "required_documents": documents,
        "submission": {
            "method": method,
            "instructions": instructions,
            "mailing_fallback_allowed": mailing,
        },
        "document_receipt": {"status": receipt, "received_at": received},
        "assigned_representative": (
            {"name": rep_name, "contact": rep_contact} if rep_name else None
        ),
        "record_version": version,
    }


class SupabaseClaimsRepository:
    """Never pass this secret-bearing adapter to browser or model code."""

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
        self._root = url.rstrip("/") + "/rest/v1"
        self._key = secret_key
        self._client = client

    async def _rows(self, table: str, params: dict[str, str]) -> list[dict]:
        for attempt in range(2):
            try:
                response = await self._client.get(
                    f"{self._root}/{table}",
                    params=params,
                    headers={"apikey": self._key},
                    timeout=2.0,
                )
            except httpx.TimeoutException as exc:
                if attempt == 0:
                    continue
                raise PolicyError("UPSTREAM_TIMEOUT") from exc
            except httpx.RequestError as exc:
                if attempt == 0:
                    continue
                raise PolicyError("UPSTREAM_UNAVAILABLE") from exc
            if response.status_code in {401, 403}:
                raise PolicyError("PERMISSION_DENIED")
            if response.status_code == 429:
                raise PolicyError("RATE_LIMITED")
            if response.status_code >= 500 and attempt == 0:
                continue
            if response.status_code >= 400:
                raise PolicyError("UPSTREAM_UNAVAILABLE")
            try:
                rows = response.json()
            except ValueError as exc:
                raise PolicyError("VALIDATION_ERROR") from exc
            if not isinstance(rows, list) or any(not isinstance(r, dict) for r in rows):
                raise PolicyError("VALIDATION_ERROR")
            return rows
        raise PolicyError("UPSTREAM_UNAVAILABLE")

    async def find_by_phone_token(self, token: str) -> CustomerCandidate | None:
        rows = await self._rows(
            "customers",
            {
                "select": "customer_id,postal_lookup_hmac,is_active",
                "phone_lookup_hmac": f"eq.{token}",
                "limit": "1",
            },
        )
        if not rows:
            return None
        row = rows[0]
        try:
            UUID(row["customer_id"])
            postal = row["postal_lookup_hmac"]
            active = row["is_active"]
        except (KeyError, TypeError, ValueError) as exc:
            raise PolicyError("VALIDATION_ERROR") from exc
        if not isinstance(postal, str) or len(postal) != 64 or type(active) is not bool:
            raise PolicyError("VALIDATION_ERROR")
        return CustomerCandidate(row["customer_id"], postal) if active else None

    async def load_faq_snapshot(self) -> FaqSnapshot:
        """Load the entire pinned FAQ once, before serving caller turns."""

        rows = await self._rows(
            "faq_entries",
            {
                "select": "topic_id,version,answer_text,access_class,effective_from,"
                "effective_to,is_active",
                "version": f"eq.{KB_VERSION}",
                "is_active": "eq.true",
            },
        )
        return FaqSnapshot.from_rows(rows)

    async def find_claim_for_candidate(
        self, customer_ref: str, claim_token: str
    ) -> str | None:
        rows = await self._rows(
            "claims",
            {
                "select": "claim_pk",
                "customer_id": f"eq.{customer_ref}",
                "claim_lookup_hmac": f"eq.{claim_token}",
                "limit": "1",
            },
        )
        if not rows:
            return None
        try:
            claim_ref = rows[0]["claim_pk"]
            UUID(claim_ref)
        except (KeyError, TypeError, ValueError) as exc:
            raise PolicyError("VALIDATION_ERROR") from exc
        return claim_ref

    async def get_claim_status(self, state: SessionState) -> dict:
        """The model supplies no claim/customer reference or authorization flag."""

        claim_ref = state.require_claim_ref()
        generation = state.request_generation
        rows = await self._rows(
            "claims",
            {"select": _CLAIM_FIELDS, "claim_pk": f"eq.{claim_ref}", "limit": "1"},
        )
        state.reject_stale(generation)
        state.require_claim_ref()
        if len(rows) != 1:
            raise PolicyError("UPSTREAM_UNAVAILABLE")
        return claim_projection(rows[0])

    async def get_verified_caller_name(self, state: SessionState) -> str:
        """Fetch a synthetic display name only for a server-verified caller."""

        customer_ref = state.verified_customer_ref
        if state.auth != Auth.VERIFIED or not customer_ref:
            raise PolicyError("UNAUTHENTICATED")
        try:
            UUID(customer_ref)
        except (TypeError, ValueError) as exc:
            raise PolicyError("VALIDATION_ERROR") from exc
        rows = await self._rows(
            "customers",
            {
                "select": "display_name",
                "customer_id": f"eq.{customer_ref}",
                "limit": "1",
            },
        )
        if len(rows) != 1:
            raise PolicyError("UPSTREAM_UNAVAILABLE")
        name = rows[0].get("display_name")
        if not isinstance(name, str) or not re.fullmatch(
            r"[A-Za-z][A-Za-z .'-]{0,99}", name
        ):
            raise PolicyError("VALIDATION_ERROR")
        return name
