import json
from uuid import uuid4

import httpx
import pytest

from airtable_writer import AirtableWriter, DeliveryError
from errors import PolicyError
from outbox import InteractionDelivery, SupabaseOutbox
from postcall import Interaction


def interaction() -> Interaction:
    return Interaction(
        call_id=str(uuid4()),
        caller_name="unknown/unverified",
        summary="The caller received approved general guidance.",
        sentiment="neutral",
        timestamp_utc="2026-09-27T09:00:00+00:00",
        outcome="faq_resolved",
        authenticated=False,
    )


@pytest.mark.asyncio
async def test_outbox_precedes_one_airtable_upsert() -> None:
    calls = []

    def supabase(request: httpx.Request) -> httpx.Response:
        calls.append(("supabase", request.method))
        assert request.headers["apikey"] == "sb_secret_synthetic-test"
        if request.method == "POST":
            assert request.headers["prefer"] == "return=representation"
            return httpx.Response(201, json=[{"outbox_id": "opaque"}])
        assert request.url.params["delivery_state"] == "eq.QUEUED"
        assert request.headers["prefer"] == "return=representation"
        assert json.loads(request.content)["delivery_state"] == "DELIVERED"
        return httpx.Response(200, json=[{"outbox_id": "opaque"}])

    def airtable(request: httpx.Request) -> httpx.Response:
        calls.append(("airtable", request.method))
        assert request.headers["authorization"] == "Bearer patSynthetic"
        body = json.loads(request.content)
        assert body["performUpsert"]["fieldsToMergeOn"] == ["Call ID"]
        assert body["records"][0]["fields"]["Call ID"] == item.call_id
        return httpx.Response(200, json={"records": [{"id": "recSynthetic"}]})

    item = interaction()
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(supabase)) as supabase_client,
        httpx.AsyncClient(transport=httpx.MockTransport(airtable)) as airtable_client,
    ):
        service = InteractionDelivery(
            SupabaseOutbox(
                "https://example.supabase.co",
                "sb_secret_synthetic-test",
                supabase_client,
            ),
            AirtableWriter(
                "patSynthetic", "appSynthetic", "tblSynthetic", airtable_client
            ),
        )
        assert await service.queue_and_attempt(item) == "delivered"
    assert calls == [
        ("supabase", "POST"),
        ("airtable", "PATCH"),
        ("supabase", "PATCH"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("airtable_status", "expected"),
    [(429, "retry_pending"), (403, "dead_letter")],
)
async def test_delivery_failures_remain_in_outbox(
    airtable_status: int, expected: str
) -> None:
    marked = []

    def supabase(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json=[{}])
        marked.append(json.loads(request.content)["delivery_state"])
        return httpx.Response(200, json=[{}])

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(supabase)) as supabase_client,
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(airtable_status)
            )
        ) as airtable_client,
    ):
        service = InteractionDelivery(
            SupabaseOutbox(
                "https://example.supabase.co",
                "sb_secret_synthetic-test",
                supabase_client,
            ),
            AirtableWriter(
                "patSynthetic", "appSynthetic", "tblSynthetic", airtable_client
            ),
        )
        assert await service.queue_and_attempt(interaction()) == expected
    assert marked == [expected.upper()]


@pytest.mark.asyncio
async def test_uncertain_write_can_reconcile_without_second_patch() -> None:
    item = interaction()
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        assert request.url.params["filterByFormula"] == (
            f'{{Call ID}}="{item.call_id}"'
        )
        return httpx.Response(
            200,
            json={
                "records": [{"id": "recExisting", "fields": {"Call ID": item.call_id}}]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        writer = AirtableWriter("patSynthetic", "appSynthetic", "tblSynthetic", client)
        assert await writer.deliver(item, reconcile_first=True) == "recExisting"
    assert calls == ["GET"]


@pytest.mark.asyncio
async def test_airtable_timeout_leaves_uncertain_outbox_row() -> None:
    marked = []

    def supabase(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(201, json=[{}])
        marked.append(json.loads(request.content)["delivery_state"])
        return httpx.Response(200, json=[{}])

    def timed_out(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(supabase)) as supabase_client,
        httpx.AsyncClient(transport=httpx.MockTransport(timed_out)) as airtable_client,
    ):
        service = InteractionDelivery(
            SupabaseOutbox(
                "https://example.supabase.co",
                "sb_secret_synthetic-test",
                supabase_client,
            ),
            AirtableWriter(
                "patSynthetic", "appSynthetic", "tblSynthetic", airtable_client
            ),
        )
        assert await service.queue_and_attempt(interaction()) == "uncertain"
    assert marked == ["UNCERTAIN"]


@pytest.mark.asyncio
async def test_duplicate_airtable_records_are_conflict() -> None:
    item = interaction()
    records = [{"id": f"rec{i}", "fields": {"Call ID": item.call_id}} for i in range(2)]
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"records": records})
        )
    ) as client:
        writer = AirtableWriter("patSynthetic", "appSynthetic", "tblSynthetic", client)
        with pytest.raises(DeliveryError, match="CONFLICT"):
            await writer.deliver(item, reconcile_first=True)


@pytest.mark.asyncio
async def test_outbox_rejects_factor_in_summary_before_request() -> None:
    item = interaction()
    item = Interaction(
        **{**item.__dict__, "summary": "Claim CLM-482731 was discussed."}
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: pytest.fail("no network"))
    ) as client:
        outbox = SupabaseOutbox(
            "https://example.supabase.co", "sb_secret_synthetic-test", client
        )
        with pytest.raises(PolicyError, match="VALIDATION_ERROR"):
            await outbox.enqueue(item)
