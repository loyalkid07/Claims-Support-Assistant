"""Seed only the local synthetic fixtures; source factors stay in ignored context bin.

Run `uv run python scripts/seed_demo.py` to validate, or add `--apply` after the
Supabase migration is applied and .env.local is configured.
"""

import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv

from faq import FaqSnapshot
from identifiers import (
    lookup_token,
    normalize_claim_id,
    normalize_phone,
    normalize_postal,
)

ROOT = Path(__file__).resolve().parents[1]
FACTORS = ROOT / "context bin" / "fixtures" / "demo_factors.json"
FAQ_SOURCE = ROOT / "knowledge" / "faq_v3.json"
CASES = {
    "maya": {
        "claim_type": "AUTO_PHYSICAL_DAMAGE",
        "loss_date": "2026-09-18",
        "status_code": "AWAITING_DOCUMENTS",
        "status_display": "Awaiting documents",
        "last_updated_at": "2026-09-24T12:00:00+00:00",
        "next_action": "Provide a repair estimate and two vehicle damage photographs",
        "required_documents": ["Repair estimate", "Two vehicle damage photographs"],
        "submission_method": "UNKNOWN",
        "submission_instructions": "Ask a claims representative for the verified secure submission method. This assistant cannot accept documents on the call.",
        "mailing_fallback_allowed": False,
        "document_receipt_status": "NOT_CONFIRMED",
        "document_received_at": None,
        "estimated_next_step_at": None,
        "record_version": 2,
    },
    "eli": {
        "claim_type": "AUTO_PHYSICAL_DAMAGE",
        "loss_date": "2026-09-19",
        "status_code": "IN_REVIEW",
        "status_display": "In review",
        "last_updated_at": "2026-09-25T12:00:00+00:00",
        "next_action": "No action is currently requested",
        "required_documents": [],
        "submission_method": "NONE",
        "submission_instructions": "No documents are currently requested.",
        "mailing_fallback_allowed": False,
        "document_receipt_status": "NOT_CONFIRMED",
        "document_received_at": None,
        "estimated_next_step_at": None,
        "record_version": 2,
    },
    "noor": {
        "claim_type": "AUTO_PHYSICAL_DAMAGE",
        "loss_date": "2026-09-20",
        "status_code": "IN_REVIEW",
        "status_display": "In review",
        "last_updated_at": "2026-09-25T12:00:00+00:00",
        "next_action": "A claims specialist will review the documents received",
        "required_documents": [],
        "submission_method": "NONE",
        "submission_instructions": "No further upload is requested at this time.",
        "mailing_fallback_allowed": False,
        "document_receipt_status": "RECEIVED",
        "document_received_at": "2026-09-25T11:30:00+00:00",
        "estimated_next_step_at": None,
        "record_version": 2,
    },
    "avery": {
        "claim_type": "AUTO_PHYSICAL_DAMAGE",
        "loss_date": "2026-09-22",
        "status_code": "AWAITING_DOCUMENTS",
        "status_display": "Awaiting documents",
        "last_updated_at": "2026-09-26T14:00:00+00:00",
        "next_action": "Provide the outstanding repair estimate; vehicle damage photos are already recorded as received",
        "required_documents": ["Repair estimate"],
        "submission_method": "UNKNOWN",
        "submission_instructions": "Ask a claims representative for the verified secure submission method. This assistant cannot accept documents on the call.",
        "mailing_fallback_allowed": False,
        "document_receipt_status": "PARTIAL",
        "document_received_at": "2026-09-25T15:30:00+00:00",
        "estimated_next_step_at": None,
    },
    "samira": {
        "claim_type": "AUTO_PHYSICAL_DAMAGE",
        "loss_date": "2026-09-23",
        "status_code": "IN_REVIEW",
        "status_display": "In review",
        "last_updated_at": "2026-09-26T16:00:00+00:00",
        "next_action": "A claims specialist is arranging an inspection of the vehicle damage",
        "required_documents": [],
        "submission_method": "NONE",
        "submission_instructions": "No documents are currently requested.",
        "mailing_fallback_allowed": False,
        "document_receipt_status": "NOT_CONFIRMED",
        "document_received_at": None,
        "estimated_next_step_at": "2026-09-30T14:00:00+00:00",
    },
    "rene": {
        "claim_type": "AUTO_PHYSICAL_DAMAGE",
        "loss_date": "2026-09-03",
        "status_code": "CLOSED",
        "status_display": "Closed",
        "last_updated_at": "2026-09-24T13:00:00+00:00",
        "next_action": "No action is currently requested",
        "required_documents": [],
        "submission_method": "NONE",
        "submission_instructions": "No documents are currently requested.",
        "mailing_fallback_allowed": False,
        "document_receipt_status": "NOT_CONFIRMED",
        "document_received_at": None,
        "estimated_next_step_at": None,
    },
}

SECONDARY_CASES = {
    "maya": {
        "claim_type": "AUTO_PHYSICAL_DAMAGE",
        "loss_date": "2026-08-10",
        "status_code": "CLOSED",
        "status_display": "Closed",
        "last_updated_at": "2026-09-20T13:00:00+00:00",
        "next_action": "No action is currently requested",
        "required_documents": [],
        "submission_method": "NONE",
        "submission_instructions": "No documents are currently requested.",
        "mailing_fallback_allowed": False,
        "document_receipt_status": "NOT_CONFIRMED",
        "document_received_at": None,
        "estimated_next_step_at": None,
    }
}


def build_rows(factors: dict, secret: bytes) -> tuple[list[dict], list[dict]]:
    if set(factors) != set(CASES):
        raise ValueError("synthetic fixture set is incomplete")
    customers = []
    claims = []
    for key, case in CASES.items():
        source = factors[key]
        customer_id = source["customer_id"]
        customers.append(
            {
                "customer_id": customer_id,
                "display_name": source["display_name"],
                "phone_lookup_hmac": lookup_token(
                    secret, "phone", normalize_phone(source["phone"])
                ),
                "postal_lookup_hmac": lookup_token(
                    secret, "postal", normalize_postal(source["postal"])
                ),
                "is_active": True,
            }
        )
        claims.append(
            {
                "claim_pk": source["claim_pk"],
                "customer_id": customer_id,
                "claim_lookup_hmac": lookup_token(
                    secret, "claim", normalize_claim_id(source["claim_id"])
                ),
                "claim_id_display": normalize_claim_id(source["claim_id"]),
                "assigned_representative_name": None,
                "assigned_representative_contact": None,
                "record_version": case.get("record_version", 1),
                **case,
            }
        )
        if key in SECONDARY_CASES:
            secondary = SECONDARY_CASES[key]
            claims.append(
                {
                    "claim_pk": source["secondary_claim_pk"],
                    "customer_id": customer_id,
                    "claim_lookup_hmac": lookup_token(
                        secret,
                        "claim",
                        normalize_claim_id(source["secondary_claim_id"]),
                    ),
                    "claim_id_display": normalize_claim_id(
                        source["secondary_claim_id"]
                    ),
                    "assigned_representative_name": None,
                    "assigned_representative_contact": None,
                    "record_version": 1,
                    **secondary,
                }
            )
    return customers, claims


def faq_rows() -> list[dict]:
    """Load the one reviewer-visible FAQ source and reject incomplete content."""

    rows = json.loads(FAQ_SOURCE.read_text(encoding="utf-8"))
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("FAQ source must be a list of entries")
    FaqSnapshot.from_rows(rows)
    return rows


async def apply_rows(
    url: str, key: str, tables: tuple[list[dict], list[dict], list[dict]]
) -> None:
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not parsed.hostname.endswith(".supabase.co")
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid Supabase URL")
    if not key.startswith("sb_secret_"):
        raise ValueError("expected a server-only Supabase secret key")
    async with httpx.AsyncClient(timeout=10.0) as client:
        for table, rows, conflict in zip(
            ("customers", "claims", "faq_entries"),
            tables,
            ("customer_id", "claim_pk", "topic_id,version"),
            strict=True,
        ):
            response = await client.post(
                f"{url.rstrip('/')}/rest/v1/{table}",
                params={"on_conflict": conflict},
                headers={
                    "apikey": key,
                    "Prefer": "resolution=merge-duplicates,return=minimal",
                },
                json=rows,
            )
            if response.status_code not in {200, 201, 204}:
                raise RuntimeError(
                    f"synthetic seed failed for {table}: HTTP {response.status_code}"
                )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply", action="store_true", help="write to configured Supabase project"
    )
    args = parser.parse_args()
    load_dotenv(ROOT / ".env.local")
    factors = json.loads(FACTORS.read_text(encoding="utf-8"))
    secret = os.environ.get("IDENTIFIER_HMAC_SECRET", "").encode()
    customers, claims = build_rows(factors, secret)
    faqs = faq_rows()
    print(
        f"Validated {len(customers)} synthetic customers, {len(claims)} claims, {len(faqs)} FAQs."
    )
    if args.apply:
        import asyncio

        asyncio.run(
            apply_rows(
                os.environ["SUPABASE_URL"],
                os.environ["SUPABASE_SECRET_KEY"],
                (customers, claims, faqs),
            )
        )
        print("Synthetic rows submitted to Supabase.")


if __name__ == "__main__":
    main()
