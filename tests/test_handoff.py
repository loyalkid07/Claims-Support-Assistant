import base64
import json
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import httpx
import pytest

from errors import PolicyError
from handoff import (
    HANDOFF_WAIT_SECONDS,
    HandoffRequest,
    SupabaseHandoffRepository,
    minimum_handoff_summary,
)
from postcall import TrustedEvent
from scripts import join_supervisor, watch_handoffs
from scripts.join_supervisor import display_summary, meet_join_url
from session_state import Handoff, SessionState
from supervisor_broker import SupervisorBroker, WaitingHandoff
from tests.test_voice_workflow import _workflow


def _jwt_payload(token: str) -> dict:
    payload = token.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))


def test_unverified_handoff_summary_has_only_minimum_context() -> None:
    state = SessionState(room_name="opaque-room")
    summary = minimum_handoff_summary(
        state, (TrustedEvent.FAQ_ANSWERED,), "Unverified Name"
    )
    assert summary["caller_name"] == "unknown/unverified"
    assert summary["verification_state"] == "UNVERIFIED"
    assert summary["completed_actions"] == ["public FAQ answered"]
    assert "claim_id" not in summary
    assert "Unverified Name" not in str(summary)


def test_handoff_reason_is_optional_bounded_context() -> None:
    state = SessionState(room_name="opaque-room")
    assert minimum_handoff_summary(state, (), None)["caller_requested_reason"] is None
    summary = minimum_handoff_summary(state, (), None, "documents")
    assert summary["caller_requested_reason"] == "documents"
    with pytest.raises(PolicyError, match="INVALID_INPUT"):
        minimum_handoff_summary(state, (), None, "raw caller narrative")


@pytest.mark.asyncio
async def test_handoff_rejects_wrong_room_role_and_identity_then_connects() -> None:
    handoff_id = str(uuid4())

    class FakeHandoffs:
        def __init__(self):
            self.connected = []

        async def create_waiting(self, state, events, caller_name, reason_category):
            assert TrustedEvent.HANDOFF_REQUESTED in events
            assert reason_category is None
            return HandoffRequest(
                handoff_id,
                state.room_name,
                datetime.now(timezone.utc) + timedelta(seconds=15),
            )

        async def mark_connected(self, value, identity):
            self.connected.append((value, identity))
            return identity == "supervisor-expected"

        async def mark_expired(self, value):
            return True

    workflow = _workflow()
    handoffs = FakeHandoffs()
    workflow.handoffs = handoffs
    workflow.state.accept_consent()
    workflow.caller_turn_committed("I want to talk to a representative")
    assert (await workflow.request_representative())["status"] == "waiting"
    assert workflow.state.handoff == Handoff.WAITING
    valid_attrs = {"role": "supervisor", "handoff_id": handoff_id}
    assert not await workflow.supervisor_joined(
        "wrong-room", "supervisor-expected", valid_attrs
    )
    assert not await workflow.supervisor_joined(
        "synthetic-room",
        "supervisor-expected",
        {"role": "caller", "handoff_id": handoff_id},
    )
    assert not await workflow.supervisor_joined(
        "synthetic-room", "supervisor-wrong", valid_attrs
    )
    assert TrustedEvent.HANDOFF_CONNECTED not in workflow.events
    assert await workflow.supervisor_joined(
        "synthetic-room", "supervisor-expected", valid_attrs
    )
    assert workflow.state.handoff == Handoff.CONNECTED
    assert workflow.events.count(TrustedEvent.HANDOFF_CONNECTED) == 1
    assert not await workflow.expire_handoff(handoff_id)


@pytest.mark.asyncio
async def test_handoff_timeout_never_claims_connection() -> None:
    class FakeHandoffs:
        async def create_waiting(self, state, events, caller_name, reason_category):
            return HandoffRequest(
                str(uuid4()),
                state.room_name,
                datetime.now(timezone.utc) + timedelta(seconds=15),
            )

        async def mark_expired(self, handoff_id):
            return True

    workflow = _workflow()
    workflow.handoffs = FakeHandoffs()
    workflow.state.accept_consent()
    workflow.caller_turn_committed("I want a person")
    result = await workflow.request_representative()
    assert await workflow.expire_handoff(result["handoff_id"])
    assert workflow.state.handoff == Handoff.FAILED
    assert TrustedEvent.HANDOFF_UNAVAILABLE in workflow.events
    assert TrustedEvent.HANDOFF_CONNECTED not in workflow.events


@pytest.mark.asyncio
async def test_unsupported_reason_never_blocks_requested_handoff() -> None:
    class FakeHandoffs:
        async def create_waiting(self, state, events, caller_name, reason_category):
            assert reason_category is None
            return HandoffRequest(
                str(uuid4()),
                state.room_name,
                datetime.now(timezone.utc) + timedelta(seconds=15),
            )

    workflow = _workflow()
    workflow.handoffs = FakeHandoffs()
    workflow.state.accept_consent()
    workflow.caller_turn_committed("I want a representative")
    result = await workflow.request_representative("raw caller narrative")
    assert result["status"] == "waiting"


@pytest.mark.asyncio
async def test_handoff_repository_creates_sanitized_waiting_row() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/handoff_requests")
        row = json.loads(request.content)
        assert row["state"] == "WAITING"
        assert row["expected_role"] == "supervisor"
        assert row["summary_json"]["caller_name"] == "unknown/unverified"
        assert row["summary_json"]["caller_requested_reason"] == "documents"
        return httpx.Response(201, json=[{"handoff_id": row["handoff_id"]}])

    state = SessionState(room_name="opaque-room")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        repo = SupabaseHandoffRepository(
            "https://example.supabase.co", "sb_secret_test", client
        )
        request = await repo.create_waiting(state, (), None, "documents")
    assert request.room_name == "opaque-room"
    remaining = (request.expires_at_utc - datetime.now(timezone.utc)).total_seconds()
    assert HANDOFF_WAIT_SECONDS - 5 <= remaining <= HANDOFF_WAIT_SECONDS


def test_broker_claims_one_waiting_room_and_mints_short_scoped_token() -> None:
    handoff_id = str(uuid4())
    expires = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    row = {
        "handoff_id": handoff_id,
        "room_name": "opaque-room",
        "state": "WAITING",
        "expires_at": expires,
        "summary_json": {"call_id": str(uuid4()), "completed_actions": []},
    }
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        if request.method == "GET":
            assert request.url.params["supervisor_identity"] == "is.null"
            return httpx.Response(200, json=[row])
        assert request.url.params["state"] == "eq.WAITING"
        assert request.url.params["supervisor_identity"] == "is.null"
        assert json.loads(request.content)["supervisor_identity"].startswith(
            "supervisor-"
        )
        return httpx.Response(
            200,
            json=[
                {
                    **row,
                    "supervisor_identity": json.loads(request.content)[
                        "supervisor_identity"
                    ],
                }
            ],
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        broker = SupervisorBroker(
            "https://example.supabase.co",
            "sb_secret_test",
            "wss://example.livekit.cloud",
            "synthetic-key",
            "synthetic-secret-32-chars-minimum",
            client,
        )
        result = broker.issue_token(handoff_id)
    assert calls == ["GET", "PATCH"]
    assert result["url"] == "wss://example.livekit.cloud"
    claims = _jwt_payload(result["token"])
    assert claims["video"]["room"] == "opaque-room"
    assert claims["video"]["roomJoin"] is True
    assert not claims["video"].get("roomAdmin", False)
    assert claims["attributes"] == {"role": "supervisor", "handoff_id": handoff_id}
    assert 0 < claims["exp"] - int(datetime.now(timezone.utc).timestamp()) <= 60


def test_cli_uses_official_meet_custom_route_and_minimum_summary(capsys) -> None:
    url = meet_join_url("wss://example.livekit.cloud", "header.payload.signature")
    parsed = urlparse(url)
    assert parsed.scheme == "https"
    assert parsed.netloc == "meet.livekit.io"
    assert parsed.path == "/custom"
    assert parse_qs(parsed.query) == {
        "liveKitUrl": ["wss://example.livekit.cloud"],
        "token": ["header.payload.signature"],
    }

    waiting = WaitingHandoff(
        str(uuid4()),
        "opaque-room",
        datetime.now(timezone.utc) + timedelta(seconds=15),
        {
            "call_id": "synthetic-call",
            "caller_name": "Unverified Name",
            "verification_state": "UNVERIFIED",
            "completed_actions": ["public FAQ answered"],
            "raw_factor": "must-not-print",
        },
    )
    display_summary(waiting)
    output = capsys.readouterr().out
    assert "synthetic-call" in output
    assert "UNVERIFIED" in output
    assert "public FAQ answered" in output
    assert "Unverified Name" not in output
    assert "must-not-print" not in output


def test_cli_lists_single_waiting_request_and_prints_join_link(
    monkeypatch, capsys
) -> None:
    handoff_id = str(uuid4())
    waiting = WaitingHandoff(
        handoff_id,
        "opaque-room",
        datetime.now(timezone.utc) + timedelta(seconds=15),
        {
            "call_id": "synthetic-call",
            "verification_state": "VERIFIED",
            "completed_actions": ["claim status provided"],
        },
    )

    class FakeBroker:
        def __init__(self, *args):
            pass

        def list_waiting(self):
            return [waiting]

        def issue_token(self, selected_id):
            assert selected_id == handoff_id
            return {"url": "wss://example.livekit.cloud", "token": "fake-token"}

    for name in (
        "SUPABASE_URL",
        "SUPABASE_SECRET_KEY",
        "LIVEKIT_URL",
        "LIVEKIT_API_KEY",
        "LIVEKIT_API_SECRET",
    ):
        monkeypatch.setenv(name, "synthetic-config")
    monkeypatch.setattr(join_supervisor, "SupervisorBroker", FakeBroker)
    monkeypatch.setattr(sys, "argv", ["join_supervisor.py"])

    assert join_supervisor.main() == 0
    output = capsys.readouterr().out
    assert "synthetic-call" in output
    assert "VERIFIED" in output
    assert "claim status provided" in output
    assert "https://meet.livekit.io/custom?" in output
    assert "token=fake-token" in output


def test_watcher_alerts_once_with_summary_and_manual_link(capsys) -> None:
    waiting = WaitingHandoff(
        str(uuid4()),
        "opaque-room",
        datetime.now(timezone.utc) + timedelta(seconds=15),
        {
            "call_id": "synthetic-call",
            "caller_name": "Maya Chen",
            "verification_state": "VERIFIED",
            "completed_actions": ["claim status provided", "secret-factor"],
            "raw_factor": "must-not-print",
        },
    )

    class FakeBroker:
        def __init__(self):
            self.claims = []

        def list_waiting(self):
            return [waiting]

        def issue_token(self, handoff_id):
            self.claims.append(handoff_id)
            return {"url": "wss://example.livekit.cloud", "token": "fake-token"}

    broker = FakeBroker()
    watcher = watch_handoffs.HandoffWatcher(broker)
    watcher.poll_once()
    watcher.poll_once()
    output = capsys.readouterr().out
    assert broker.claims == [waiting.handoff_id]
    assert output.count("INCOMING CALL ESCALATION") == 1
    assert "Maya Chen" in output
    assert "opaque-room" in output
    assert "claim status provided" in output
    assert "secret-factor" not in output
    assert "must-not-print" not in output
    assert "https://meet.livekit.io/custom?" in output
    assert "token=fake-token" in output


def test_watcher_does_not_claim_two_calls_for_one_supervisor(capsys) -> None:
    first = WaitingHandoff(
        str(uuid4()),
        "room-one",
        datetime.now(timezone.utc) + timedelta(seconds=15),
        {"call_id": "call-one"},
    )
    second = WaitingHandoff(
        str(uuid4()),
        "room-two",
        datetime.now(timezone.utc) + timedelta(seconds=25),
        {"call_id": "call-two"},
    )

    class FakeBroker:
        def __init__(self):
            self.claims = []

        def list_waiting(self):
            return [first, second]

        def issue_token(self, handoff_id):
            self.claims.append(handoff_id)
            return {"url": "wss://example.livekit.cloud", "token": "fake-token"}

    broker = FakeBroker()
    watcher = watch_handoffs.HandoffWatcher(broker)
    watcher.poll_once()
    watcher.poll_once()
    assert broker.claims == [first.handoff_id]
    assert capsys.readouterr().out.count("Another handoff is waiting") == 1

    watcher._active_until = datetime.now(timezone.utc) - timedelta(seconds=1)
    watcher.poll_once()
    assert broker.claims == [first.handoff_id, second.handoff_id]


def test_watcher_check_is_read_only(monkeypatch, capsys) -> None:
    class FakeBroker:
        def list_waiting(self):
            return []

        def issue_token(self, handoff_id):
            pytest.fail("--check must never claim a handoff")

    monkeypatch.setattr(
        watch_handoffs, "configured_broker", lambda client: FakeBroker()
    )
    monkeypatch.setattr(sys, "argv", ["watch_handoffs.py", "--check"])
    assert watch_handoffs.main() == 0
    output = capsys.readouterr().out
    assert "Read OK: 0" in output
    assert "No token was issued" in output


def test_watcher_recovers_after_read_outage_without_issuing_token(
    monkeypatch, capsys
) -> None:
    class FlakyBroker:
        def __init__(self):
            self.reads = 0

        def list_waiting(self):
            self.reads += 1
            if self.reads <= 2:
                raise httpx.ConnectError("synthetic outage")
            return []

        def issue_token(self, handoff_id):
            pytest.fail("an empty read must not issue a token")

    broker = FlakyBroker()

    def stop_after_recovery(seconds):
        if broker.reads >= 3:
            raise KeyboardInterrupt

    monkeypatch.setattr(watch_handoffs.time, "sleep", stop_after_recovery)
    with pytest.raises(KeyboardInterrupt):
        watch_handoffs.HandoffWatcher(broker).run()
    output = capsys.readouterr().out
    assert output.count("Handoff read unavailable") == 1
    assert output.count("Handoff read restored") == 1
