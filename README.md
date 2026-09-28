# Claims Support Assistant

I built this VoiceAI agent for the Observe.AI AI Agent Engineer take-home. It handles inbound calls for fictional Observe Insurance: a caller can ask general claims questions, check an existing synthetic claim after verification, or ask to speak with a person. At the end of the AI portion of the call, it records a sanitized interaction.

I kept the system small enough to explain end to end. One LiveKit agent handles the conversation, while deterministic Python code owns verification, claim access, handoff state, and post-call delivery. The demo uses synthetic data only.

## Review materials

The call recordings and supporting artifacts are collected in the [submission guide](submission/README.md).

## What the caller can do

- Invited callers must receive the demo participation notice before joining or dialing. The short spoken greeting identifies the assistant as automated and invites the caller's request without a separate verbal consent turn.
- Ask public questions about hours, mailing address, starting a claim, the general process, documents, and emergencies. Answers come from a versioned twelve-topic FAQ, not unrestricted model knowledge. Its general-enquiries email is explicitly fictional and non-deliverable, never a destination for claim documents.
- Check an existing claim. The account phone is a lookup key, not proof of identity. The agent confirms it, then confirms a claim ID and ZIP/postal code together before server code allows claim-specific information. At most three completed verification bundles are accepted per call.
- Request a representative without explaining why or completing verification. A supervisor can join the same LiveKit room; the agent calls the handoff successful only after the expected participant actually joins.

The assistant does not file or adjudicate claims, interpret coverage, take payments, promise callbacks, or accept real personal information. Failed lookup and mismatched factors have the same caller-facing verification response.

## How I built it

| Responsibility | Implementation |
| --- | --- |
| Voice and room transport | LiveKit Agents with Deepgram Nova-3 STT, GPT-4.1 mini, and Cartesia Sonic-3 TTS through LiveKit Inference |
| Conversation | One agent with a versioned prompt; tool calls pass through a testable workflow layer |
| Authorization | Per-call server state, confirmed factors, attempt limits, and guarded claim reads; the LLM cannot mark a caller verified |
| Customer, claims, and FAQ data | Synthetic Supabase/Postgres tables accessed only by server-side adapters |
| Post-call record | A sanitized interaction is queued in a Supabase outbox before an immediate idempotent Airtable upsert |
| Human handoff | A terminal watcher shows a minimal summary and a short-lived, room-scoped LiveKit Meet link; no custom WebRTC frontend |

```mermaid
flowchart LR
    caller["Caller (phone or Agent Console)"] <--> room["LiveKit room"]
    agent["Python voice agent"] <--> room
    agent --> policy["Session entry, verification, guarded tools"]
    policy --> data[(Supabase data)]
    agent --> delivery["AI-segment completion"]
    delivery -->|queue first| outbox[(Supabase outbox)]
    delivery -->|then attempt upsert| airtable[(Airtable interactions)]
    agent --> request[(Supabase handoff requests)]
    request <--> watcher["Supervisor watcher"]
    watcher --> meet["LiveKit Meet"]
    meet <--> room
```

The code follows those boundaries directly: [agent.py](src/agent.py) wires the voice session using the active [prompt](src/prompts/v4.py), [voice_workflow.py](src/voice_workflow.py) connects tools to policy, [session_state.py](src/session_state.py) and [verification.py](src/verification.py) guard access, and [supabase_claims.py](src/supabase_claims.py) returns only approved claim fields. [postcall.py](src/postcall.py), [outbox.py](src/outbox.py), and [airtable_writer.py](src/airtable_writer.py) handle completion. The active FAQ source is [faq_v3.json](knowledge/faq_v3.json); schema and setup details are in [migrations](migrations/) and [integration_setup.md](docs/integration_setup.md).

I chose a small, pinned FAQ over a RAG system because these answers are finite and policy-sensitive. I keep unknown accounts and mismatched factors on the same caller-facing path to avoid revealing whether an account exists. I use LiveKit Meet for the supervisor's same-room join instead of maintaining a custom browser app.

I used an outbox because an Airtable timeout can leave delivery uncertain; the `Call ID` provides a logical idempotency key. The current implementation makes one immediate Airtable attempt and records failure state, but it does **not** run an automatic retry worker.

## Run it locally

You need Python 3.10–3.14, `uv`, a LiveKit Cloud project, and the synthetic Supabase and Airtable services described in [integration_setup.md](docs/integration_setup.md). Create `.env.local` from [.env.example](.env.example) only if it does not already exist, then fill in your own server-side credentials. Never commit that file. Use synthetic test values only; I provide the verification values separately to invited reviewers.

From the repository root:

```powershell
uv sync --locked
uv run python src/agent.py dev --log-level WARNING --no-reload
```

Before inviting anyone to a voice session, provide [the demo participation notice](docs/demo-participant-notice.md) and use synthetic test data only. For local voice testing, connect through Agent Console in the same LiveKit project and allow microphone access. The Cloud agent is also deployed; the commands above run a separate local development worker.

For a cheaper text-mode behavior check, use the LiveKit debugger instead of making a voice call:

```powershell
lk agent debugger start src/agent.py
lk agent debugger say "How do I start a new claim?"
lk agent debugger stop
```

## Verify the build

```powershell
uv run ruff format --check .
uv run ruff check .
uv run pytest -q
```

The local suite covers state and authorization guards, identifier handling, Supabase claim projection, FAQ validation, Airtable/outbox behavior, and supervisor handoff. The submission guide contains the voice-call evidence.

This is a synthetic assessment build, not a claim of regulatory compliance or production identity assurance. It has no outbound PSTN transfer, real claimant data, automatic outbox retry process, or production supervisor access control. Those are deliberate boundaries rather than features implied by the demo.
