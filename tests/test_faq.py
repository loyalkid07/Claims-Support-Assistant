from datetime import date

import httpx
import pytest

from errors import PolicyError
from faq import KB_VERSION, TOPICS, FaqSnapshot
from scripts.seed_demo import faq_rows
from supabase_claims import SupabaseClaimsRepository


def _rows() -> list[dict]:
    return [
        {
            "topic_id": topic,
            "version": KB_VERSION,
            "answer_text": f"Approved public answer for {topic}.",
            "access_class": "PUBLIC",
            "effective_from": "2026-09-26",
            "effective_to": None,
            "is_active": True,
        }
        for topic in sorted(TOPICS)
    ]


def test_complete_pinned_snapshot_and_unknown_topic() -> None:
    snapshot = FaqSnapshot.from_rows(_rows(), today=date(2026, 9, 27))
    assert snapshot.get_faq("office_hours") == {
        "status": "found",
        "topic_id": "office_hours",
        "answer_text": "Approved public answer for office_hours.",
        "version": "faq-v2",
        "effective_date": "2026-09-26",
        "access_class": "PUBLIC",
    }
    assert snapshot.get_faq("claim_status") == {
        "status": "unknown",
        "safe_action": "offer_representative",
    }


def test_reviewer_visible_faq_source_matches_seed_contract() -> None:
    rows = faq_rows()
    assert len(rows) == 11
    assert {row["topic_id"] for row in rows} == TOPICS
    assert {row["version"] for row in rows} == {KB_VERSION}
    assert all(row["source_note"] for row in rows)
    snapshot = FaqSnapshot.from_rows(rows, today=date(2026, 9, 27))
    assert (
        "fictional, non-deliverable"
        in snapshot.get_faq("mailing_address")["answer_text"]
    )
    assert "demo claim" not in snapshot.get_faq("general_documents")["answer_text"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda rows: rows.pop(),
        lambda rows: rows.append(rows[0].copy()),
        lambda rows: rows[0].update(access_class="AUTHENTICATED_GENERAL"),
        lambda rows: rows[0].update(version="faq-v1"),
        lambda rows: rows[0].update(answer_text="https://unapproved.example"),
        lambda rows: rows[0].update(effective_to="2026-09-26"),
    ],
)
def test_snapshot_rejects_incomplete_or_unapproved_rows(mutate) -> None:
    rows = _rows()
    mutate(rows)
    with pytest.raises(PolicyError):
        FaqSnapshot.from_rows(rows, today=date(2026, 9, 27))


@pytest.mark.asyncio
async def test_supabase_loads_one_versioned_snapshot() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/faq_entries")
        assert request.url.params["version"] == "eq.faq-v2"
        assert request.url.params["is_active"] == "eq.true"
        return httpx.Response(200, json=_rows())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        repo = SupabaseClaimsRepository(
            "https://example.supabase.co", "sb_secret_synthetic-test", client
        )
        snapshot = await repo.load_faq_snapshot()
    assert snapshot.get_faq("emergency")["status"] == "found"
