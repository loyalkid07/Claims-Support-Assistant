# Synthetic integration setup

The configured demo Supabase project and Airtable base have been exercised by local calls. This guide also describes how to configure a separate synthetic environment; it does not imply that a fresh clone contains the private fixture factors or service credentials. Use only fictional demo data.

## Supabase

1. Create a new Supabase project dedicated to this demo. In its SQL editor, apply [`migrations/001_schema.sql`](../migrations/001_schema.sql).
2. Confirm the five tables exist: `customers`, `claims`, `faq_entries`, `interaction_outbox`, and `handoff_requests`. RLS is enabled and the migration revokes `anon`/`authenticated` access to those tables.
3. Copy `.env.example` to ignored `.env.local`. Set `SUPABASE_URL` to the project's HTTPS URL and `SUPABASE_SECRET_KEY` to a server-only `sb_secret_...` key. Generate a distinct random `IDENTIFIER_HMAC_SECRET` of at least 32 bytes and keep it stable across seeding and agent runs. Do not paste any secret into chat or commit it.
4. Run `uv run python scripts/seed_demo.py` to validate the three private synthetic fixture records without writing. Then run `uv run python scripts/seed_demo.py --apply` to insert them. Raw fictional test factors stay in the ignored `context bin/fixtures/demo_factors.json` file; the database stores HMAC lookup tokens, not phone or ZIP values.
5. Verify one known phone lookup and one authorized claim read in a local integration test before claiming Supabase works. Check that an unauthenticated read is rejected in server code.

The seed includes three cases: required documents, no currently required documents, and documents received. The fourth mandatory demo flow uses an unknown phone that has no database row. The FAQ seed contains eleven versioned public entries. The private factor file must be supplied separately by the project owner; it is intentionally excluded from Git.

## Airtable

Create a dedicated base and one table, then create these fields with the exact spelling below:

| Field | Airtable type |
| --- | --- |
| Call ID | Single line text (primary field) |
| Caller Name | Single line text |
| Summary | Long text |
| Sentiment | Single select: `positive`, `neutral`, `negative`, `mixed`, `unknown` |
| Timestamp UTC | Date with time, UTC display |
| Outcome | Single select: `handed_off`, `handoff_unavailable`, `claim_status_provided`, `verification_unsuccessful`, `faq_resolved`, `caller_disconnected`, `ended` |
| Authenticated | Checkbox |
| Handoff Reason | Long text |
| Tool Error Count | Number (integer) |
| Duration Seconds | Number (integer) |
| Agent Version | Single line text |
| Prompt Version | Single line text |
| Workflow Version | Single line text |
| KB Version | Single line text |
| Model Stack | Single line text |
| Trace ID | Single line text |

Create a personal access token limited to this base with `data.records:read` and `data.records:write`. Set `AIRTABLE_PAT`, `AIRTABLE_BASE_ID`, and `AIRTABLE_TABLE_ID` only in `.env.local`. The writer uses `Call ID` as its logical upsert key. Airtable does not enforce uniqueness on that text field; the reconciliation path detects multiple matches and reports a conflict.

The application queues a sanitized record in Supabase before one immediate Airtable upsert. Failed or uncertain attempts remain marked in the outbox. The shutdown completion hook has been exercised by live browser calls, and successful supervisor handoff now starts AI-segment completion after the agent's introduction without waiting for job shutdown. An automatic retry worker is **not yet implemented**; do not claim automatic eventual delivery until that worker is built and exercised with the live services.
