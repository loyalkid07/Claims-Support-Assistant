-- Synthetic demo only. Apply in the Supabase SQL editor after creating a project.
-- No raw phone, claim-verification answer, ZIP, or production claimant data belongs here.

create table if not exists public.customers (
    customer_id uuid primary key default gen_random_uuid(),
    display_name text not null,
    phone_lookup_hmac text not null unique check (phone_lookup_hmac ~ '^[0-9a-f]{64}$'),
    postal_lookup_hmac text not null check (postal_lookup_hmac ~ '^[0-9a-f]{64}$'),
    is_active boolean not null default true,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create table if not exists public.claims (
    claim_pk uuid primary key default gen_random_uuid(),
    customer_id uuid not null references public.customers(customer_id),
    claim_lookup_hmac text not null unique check (claim_lookup_hmac ~ '^[0-9a-f]{64}$'),
    claim_id_display text not null unique check (claim_id_display ~ '^CLM-[0-9]{6}$'),
    status_code text not null check (status_code in ('AWAITING_DOCUMENTS', 'IN_REVIEW', 'DOCUMENTS_RECEIVED', 'CLOSED')),
    status_display text not null,
    last_updated_at timestamptz not null,
    next_action text,
    required_documents jsonb not null default '[]'::jsonb check (jsonb_typeof(required_documents) = 'array'),
    submission_method text not null check (submission_method in ('SECURE_UPLOAD_ALREADY_ISSUED', 'MAIL', 'NONE', 'UNKNOWN')),
    submission_instructions text not null,
    mailing_fallback_allowed boolean not null default false,
    document_receipt_status text not null check (document_receipt_status in ('RECEIVED', 'PARTIAL', 'NOT_CONFIRMED')),
    document_received_at timestamptz,
    estimated_next_step_at timestamptz,
    assigned_representative_name text,
    assigned_representative_contact text,
    record_version integer not null default 1 check (record_version > 0)
);
create index if not exists claims_customer_id_idx on public.claims(customer_id);

create table if not exists public.faq_entries (
    topic_id text not null,
    version text not null,
    answer_text text not null,
    access_class text not null check (access_class in ('PUBLIC', 'AUTHENTICATED_GENERAL')),
    effective_from date not null,
    effective_to date,
    source_note text not null,
    is_active boolean not null default true,
    primary key (topic_id, version),
    check (effective_to is null or effective_to >= effective_from)
);
create unique index if not exists faq_one_active_topic_idx on public.faq_entries(topic_id) where is_active;

create table if not exists public.interaction_outbox (
    outbox_id uuid primary key default gen_random_uuid(),
    call_id uuid not null unique,
    payload jsonb not null check (jsonb_typeof(payload) = 'object' and payload ?& array['call_id','caller_name','summary','sentiment','timestamp_utc','outcome','authenticated']),
    payload_hash text not null check (payload_hash ~ '^[0-9a-f]{64}$'),
    delivery_state text not null default 'QUEUED' check (delivery_state in ('QUEUED','IN_FLIGHT','DELIVERED','RETRY_PENDING','UNCERTAIN','DEAD_LETTER')),
    attempt_count integer not null default 0 check (attempt_count >= 0),
    next_attempt_at timestamptz,
    lease_owner text,
    lease_expires_at timestamptz,
    last_error_code text,
    last_error_at timestamptz,
    airtable_record_id text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    delivered_at timestamptz
);
create index if not exists outbox_due_idx on public.interaction_outbox(next_attempt_at)
    where delivery_state in ('QUEUED','RETRY_PENDING','UNCERTAIN');

create table if not exists public.handoff_requests (
    handoff_id uuid primary key default gen_random_uuid(),
    call_id uuid not null,
    room_name text not null,
    state text not null check (state in ('WAITING','CONNECTED','EXPIRED','CANCELLED')),
    summary_json jsonb not null check (jsonb_typeof(summary_json) = 'object'),
    expected_role text not null default 'supervisor' check (expected_role = 'supervisor'),
    expires_at timestamptz not null,
    supervisor_identity text,
    connected_at timestamptz,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
create unique index if not exists handoff_one_active_call_idx on public.handoff_requests(call_id)
    where state in ('WAITING','CONNECTED');

alter table public.customers enable row level security;
alter table public.claims enable row level security;
alter table public.faq_entries enable row level security;
alter table public.interaction_outbox enable row level security;
alter table public.handoff_requests enable row level security;

revoke all on public.customers, public.claims, public.faq_entries,
    public.interaction_outbox, public.handoff_requests from anon, authenticated;
grant select, insert, update on public.customers, public.claims, public.faq_entries,
    public.interaction_outbox, public.handoff_requests to service_role;
