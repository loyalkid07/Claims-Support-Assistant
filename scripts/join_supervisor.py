"""Claim a waiting handoff and print a one-time LiveKit Meet join link.

Run from a trusted operator terminal during the caller's 60-second handoff wait.
The link embeds a short-lived access token; never paste it into logs or chat.
"""

import argparse
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx
from dotenv import load_dotenv

from supervisor_broker import SupervisorBroker, WaitingHandoff

ROOT = Path(__file__).resolve().parents[1]
MEET_CUSTOM_URL = "https://meet.livekit.io/custom"


def meet_join_url(livekit_url: str, token: str) -> str:
    """Use the official Meet custom-room route, which needs both parameters."""

    return f"{MEET_CUSTOM_URL}?{urlencode({'liveKitUrl': livekit_url, 'token': token})}"


def display_summary(waiting: WaitingHandoff) -> None:
    summary = waiting.summary
    remaining = max(
        0, int((waiting.expires_at - datetime.now(timezone.utc)).total_seconds())
    )
    actions = summary.get("completed_actions")
    if not isinstance(actions, list):
        actions = []
    approved_actions = {
        "public FAQ answered",
        "claim status provided",
        "verification unsuccessful",
    }
    actions = [
        action
        for action in actions
        if isinstance(action, str) and action in approved_actions
    ]
    caller = (
        summary.get("caller_name", "unknown/unverified")
        if summary.get("verification_state") == "VERIFIED"
        else "unknown/unverified"
    )
    if not isinstance(caller, str):
        caller = "unknown/unverified"
    print(f"Handoff ID: {waiting.handoff_id}")
    print(f"Call ID: {summary.get('call_id', 'unknown')}")
    print(f"Caller: {caller}")
    print(f"Verification: {summary.get('verification_state', 'unknown')}")
    print(f"Completed actions: {', '.join(map(str, actions)) or 'none'}")
    print(f"Room: {waiting.room_name}")
    print(f"Time remaining: ~{remaining}s")


def configured_broker(client: httpx.Client) -> SupervisorBroker:
    """Keep project secrets in the trusted operator process, never the browser."""

    load_dotenv(ROOT / ".env.local")

    required = (
        "SUPABASE_URL",
        "SUPABASE_SECRET_KEY",
        "LIVEKIT_URL",
        "LIVEKIT_API_KEY",
        "LIVEKIT_API_SECRET",
    )
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise ValueError(f"Missing local configuration: {', '.join(missing)}")

    return SupervisorBroker(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SECRET_KEY"],
        os.environ["LIVEKIT_URL"],
        os.environ["LIVEKIT_API_KEY"],
        os.environ["LIVEKIT_API_SECRET"],
        client,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--handoff-id",
        help="Select this waiting handoff when more than one is listed",
    )
    args = parser.parse_args()

    try:
        with httpx.Client() as client:
            broker = configured_broker(client)
            waiting = broker.list_waiting()
            if not waiting:
                print("No unclaimed handoff is waiting. No token was issued.")
                return 1
            print(f"{len(waiting)} waiting handoff(s):")
            for item in waiting:
                display_summary(item)
                print()

            if args.handoff_id:
                selected = next(
                    (item for item in waiting if item.handoff_id == args.handoff_id),
                    None,
                )
                if selected is None:
                    print("That handoff is not currently waiting. No token was issued.")
                    return 1
            elif len(waiting) == 1:
                selected = waiting[0]
            else:
                print("Multiple handoffs are waiting; rerun with --handoff-id ID.")
                return 1

            credentials = broker.issue_token(selected.handoff_id)
    except (httpx.HTTPError, ValueError) as exc:
        print(f"Could not issue a supervisor link ({type(exc).__name__}).")
        return 1

    print("Open this one-time link immediately in the supervisor's browser:")
    print(meet_join_url(credentials["url"], credentials["token"]))
    print("This link contains a room-scoped token. Do not share or save it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
