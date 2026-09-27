"""Watch for waiting handoffs from a trusted terminal; no browser UI or server.

The watcher claims at most one request at a time and prints a one-time Meet
link. Printing the link is not a successful handoff: the agent waits for a
validated LiveKit participant-joined event before it exits.
"""

import argparse
import time
from datetime import datetime, timezone

import httpx

from supervisor_broker import SupervisorBroker

if __package__:
    from scripts.join_supervisor import (
        configured_broker,
        display_summary,
        meet_join_url,
    )
else:
    from join_supervisor import configured_broker, display_summary, meet_join_url

POLL_SECONDS = 1.0


class HandoffWatcher:
    def __init__(self, broker: SupervisorBroker) -> None:
        self.broker = broker
        self._attempted: dict[str, datetime] = {}
        self._deferred: set[str] = set()
        self._active_until: datetime | None = None

    def poll_once(self) -> None:
        """Read durable waiting state and alert once per claim attempt."""

        waiting = self.broker.list_waiting()
        now = datetime.now(timezone.utc)
        self._attempted = {
            handoff_id: expiry
            for handoff_id, expiry in self._attempted.items()
            if expiry > now
        }
        self._deferred.intersection_update(item.handoff_id for item in waiting)
        if self._active_until is not None and self._active_until <= now:
            self._active_until = None

        for item in waiting:
            if item.handoff_id in self._attempted:
                continue
            if self._active_until is not None:
                if item.handoff_id not in self._deferred:
                    print(
                        "Another handoff is waiting, but this supervisor already "
                        "has an active link. No second token was issued."
                    )
                    self._deferred.add(item.handoff_id)
                continue

            # A timeout after the conditional PATCH is ambiguous. Never retry
            # the same claim blindly or issue a second active token.
            self._attempted[item.handoff_id] = item.expires_at
            self._active_until = item.expires_at
            try:
                credentials = self.broker.issue_token(item.handoff_id)
            except (httpx.HTTPError, ValueError) as exc:
                print(
                    f"Could not claim handoff {item.handoff_id} "
                    f"({type(exc).__name__}); no join link was issued."
                )
                continue

            print("\a", end="", flush=True)
            print("=" * 66)
            print("INCOMING CALL ESCALATION")
            display_summary(item)
            print("Open this one-time link after reading the summary:")
            print(meet_join_url(credentials["url"], credentials["token"]))
            print("Token issuance is not a connected handoff. Do not share this link.")
            print("=" * 66, flush=True)

    def run(self) -> None:
        """Poll while the operator is on duty; recover on the next good read."""

        degraded = False
        print("Watching for handoffs. Press Ctrl+C to stop.", flush=True)
        while True:
            try:
                self.poll_once()
            except (httpx.HTTPError, ValueError) as exc:
                if not degraded:
                    print(
                        f"Handoff read unavailable ({type(exc).__name__}); "
                        "retrying. No token was issued.",
                        flush=True,
                    )
                degraded = True
            else:
                if degraded:
                    print("Handoff read restored.", flush=True)
                degraded = False
            time.sleep(POLL_SECONDS)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Verify read access once without claiming a handoff or issuing a token",
    )
    args = parser.parse_args()
    try:
        with httpx.Client() as client:
            broker = configured_broker(client)
            if args.check:
                waiting = broker.list_waiting()
                print(f"Read OK: {len(waiting)} unclaimed waiting handoff(s).")
                print("No token was issued and no handoff was claimed.")
                return 0
            HandoffWatcher(broker).run()
    except KeyboardInterrupt:
        print("\nHandoff watcher stopped.")
        return 0
    except (httpx.HTTPError, ValueError) as exc:
        print(f"Could not start handoff watcher ({type(exc).__name__}).")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
