-- Add only synthetic claim context needed for a useful authenticated status read.
-- Nullable during rollout so existing calls remain readable before reseeding.

alter table public.claims
    add column if not exists claim_type text,
    add column if not exists loss_date date;

do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conrelid = 'public.claims'::regclass
          and conname = 'claims_claim_type_check'
    ) then
        alter table public.claims
            add constraint claims_claim_type_check
            check (claim_type is null or claim_type = 'AUTO_PHYSICAL_DAMAGE');
    end if;
    if not exists (
        select 1 from pg_constraint
        where conrelid = 'public.claims'::regclass
          and conname = 'claims_loss_date_check'
    ) then
        alter table public.claims
            add constraint claims_loss_date_check
            check (
                loss_date is null
                or loss_date <= (last_updated_at at time zone 'UTC')::date
            );
    end if;
end $$;
