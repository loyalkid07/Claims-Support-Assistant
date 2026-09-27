# Claims Support Assistant

Implementation workspace for the Observe.AI VoiceAI take-home assignment.

## Current status

The local agent has passed browser audio, a same-room supervisor takeover, and one inbound phone FAQ call. The phone call exercised consent, the live FAQ tool, caller disconnect, Supabase outbox delivery, and a matching Airtable record. The configured Supabase project has three synthetic customers, three claims, and the eleven-topic `faq-v1` snapshot. Deterministic server code guards verification and claim access.

This is a synthetic assessment build, not production-certified insurance software. The complete authenticated voice path and the other mandatory voice flows still need final rehearsal. The agent has not been deployed to LiveKit Cloud; the successful inbound call used a local worker. There is no automatic outbox retry worker, so a failed immediate Airtable delivery needs manual reconciliation before it can be called delivered.

The approved direction is a Python LiveKit voice agent with deterministic server-owned authorization, Supabase/Postgres for synthetic customer and claim data, Airtable for sanitized post-call records, and a same-room browser supervisor handoff.

## Local context

The ignored `context bin/` directory contains the assignment PDF and the complete private design-authority package. It is intentionally excluded from Git because it includes internal planning, interview context, working evidence, and files that are not part of the public submission.

Future private planning notes, raw review artifacts, and context exports belong in `context bin/`. Reviewer-facing documentation that is intentionally part of the submission belongs in `docs/`.

## Repository layout

```text
src/agent.py                LiveKit agent entrypoint
src/prompt_v1.py            Versioned conversational instructions
src/session_state.py        Per-call consent, authentication and access guards
src/verification.py         Confirmed-factor verification and stale-read protection
src/identifiers.py          Identifier normalization and HMAC lookup tokens
src/postcall.py             Sanitized, idempotent AI-segment fallback record
src/supabase_claims.py      Server-only lookup and guarded claim projection
src/faq.py                  Pinned eleven-topic approved FAQ snapshot
knowledge/faq_v1.json       Canonical public synthetic FAQ seed content
src/voice_workflow.py       Testable conversation-to-domain boundary
src/airtable_writer.py      Airtable upsert and uncertain-result reconciliation
src/outbox.py               Durable queue and one immediate delivery attempt
migrations/001_schema.sql   Synthetic Supabase schema and access restrictions
scripts/seed_demo.py        Synthetic seed (private factors read from ignored context)
scripts/join_supervisor.py  Trusted-terminal handoff summary and Meet link helper
scripts/watch_handoffs.py  One-second terminal watcher; no custom web UI
src/handoff.py              Sixty-second waiting request and room join state
src/supervisor_broker.py    Short-lived, room-scoped supervisor token broker
docs/integration_setup.md  Service setup and Airtable field types
tests/                     Configuration and deterministic policy tests
context bin/                Local-only design and project context; ignored by Git
```

New modules are added only when a working feature needs them. This keeps the reviewer-facing repository proportional to the implementation rather than advertising empty future architecture.

The prompt controls conversation style and tool use, not authorization. Server code
enforces consent, verification and claim access. The versioned FAQ JSON is the
single source for seeding public answers into Supabase; each voice session loads
and validates the pinned `faq-v1` snapshot from Supabase before taking calls.
The JSON contains no customer or claim records. Changing approved FAQ content
requires a new KB version and an explicit seed step; editing the file alone does
not update the live project.

## Run locally

```powershell
uv sync --locked
lk agent debugger start src/agent.py
lk agent debugger say "Hello"
lk agent debugger stop
```

For a real browser-microphone session, run this in PowerShell and leave it open:

```powershell
cd 'D:\Dev\Projects\Claims Support Assistant'
uv run python src/agent.py dev --log-level WARNING --no-reload
```

Then open LiveKit Agent Console for the same project, connect, allow microphone access, and use **synthetic fixture values only**. The configured inbound phone number can also reach this local worker while it is running. Disconnect after the test, allow the shutdown write to finish, and press `Ctrl+C` in the PowerShell worker window. Check the newest `interaction_outbox` row in Supabase and the matching `Call ID` in Airtable. Local development uses automatic dispatch; the tracked `livekit.toml` declares the intended production agent name. Do not run a second worker concurrently or leave the worker running after testing.

Run the local quality gates before a commit or deployment:

```powershell
uv run ruff format --check .
uv run ruff check .
uv run pytest -q
```

## Supervisor handoff operator workflow

Keep the voice worker running. In a second trusted PowerShell terminal, first
run `uv run python scripts/watch_handoffs.py --check` to verify read access
without issuing a token. Then run `uv run python scripts/watch_handoffs.py`
before the call. The watcher reads unclaimed Supabase handoff state once per
second. On a new waiting request, it sounds a terminal bell and prints the
call ID, verified-only caller name, verification state, completed actions,
room, remaining time, and a one-time LiveKit Meet link. It never opens a
browser automatically. Read the summary and click the link promptly, allow
microphone access, and use headphones to prevent audio feedback. Press `Ctrl+C`
to stop the watcher after the test.

The watcher claims at most one request at a time for this one-supervisor
operator workflow. An additional simultaneous request is reported but not
claimed while the first link is active. `uv run python scripts/join_supervisor.py`
remains a one-shot fallback; with multiple unclaimed requests, select one with
`--handoff-id ID`. The supervisor must join within the agent's 60-second wait;
token issuance alone is not a successful handoff. The agent confirms the
matching participant join, introduces the human, then leaves while the caller
and supervisor remain. A real joined-room/audio-continuity test has passed;
the matching post-call record was delivered to Airtable on the first attempt.

Both helpers use the [official Meet custom-room route](https://github.com/livekit-examples/meet/blob/main/app/custom/page.tsx), which requires both the LiveKit URL and token. The terminal link contains a credential; do not paste it into logs, screenshots, chat, or public evidence. This is a trusted local operator adapter, not an authenticated production supervisor console. No custom web server or browser WebRTC code is maintained here.

## Completed checkpoints

- A human browser-microphone session proved two-way LiveKit audio on 27 September 2026. The local worker closed cleanly after the caller disconnected.
- The selected STT identifier is `deepgram/nova-3`; the existing OpenAI and Cartesia identifiers remain configured. The claimed latency and recognition gains have not yet been measured in a comparable audio test.
- Phase 1 tests cover consent, confirmation, the three-attempt cap, no-match privacy, forced claim-tool access, stale lookup responses, and deterministic completion content. The Supabase outbox enforces `call_id` uniqueness durably.
- Phase 2 tests cover guarded Supabase reads, safe claim projection, a complete versioned FAQ snapshot, outbox-before-Airtable ordering, Airtable failure outcomes, reconciliation, and factor rejection from summaries. The migration and synthetic fixtures are applied in the configured Supabase project; one synthetic write reached Airtable.
- Phase 3 text-mode checks proved the deterministic opening, explicit consent tool, and live FAQ answer. Unit tests cover a full verified claim-tool path and generic unknown-customer failure. A browser FAQ call and an interrupted call each created one delivered outbox row and matching Airtable row. The authenticated browser call, failure/no-match dialogue, voice review, and verification-factor redaction review remain required.
- Supervisor handoff passed a live same-room join and human takeover. The 60-second request window, participant validation, agent exit, and first-attempt Airtable delivery were observed. The AI-segment completion now starts after the introduction rather than waiting for the human room to end; a live retest created its outbox row about 5.5 seconds after supervisor connection. This does not establish automatic retry or production supervisor authorization.
- Inbound telephony passed one local-worker FAQ call with consent and caller-initiated disconnect. The matching Airtable record was independently read. This does not establish Cloud deployment or every required phone flow.
