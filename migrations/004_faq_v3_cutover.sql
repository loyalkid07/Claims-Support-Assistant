-- Preserve faq-v2 history and atomically activate the approved faq-v3 snapshot.
-- Apply only while no agent worker is serving calls.

do $$
begin
    if (select count(*) from public.faq_entries where version = 'faq-v2') != 11 then
        raise exception 'faq-v2 is incomplete; refusing version cutover';
    end if;
    if (select count(*) from public.faq_entries where version = 'faq-v3')
       not in (0, 12) then
        raise exception 'faq-v3 is partial; refusing version cutover';
    end if;

    update public.faq_entries
    set is_active = false
    where version = 'faq-v2' and is_active;

    insert into public.faq_entries (
        topic_id, version, answer_text, access_class, effective_from,
        effective_to, source_note, is_active
    )
    select
        topic_id, 'faq-v3', answer_text, access_class, date '2026-09-28',
        null, 'Approved synthetic Observe Insurance FAQ v3', true
    from public.faq_entries
    where version = 'faq-v2'
    on conflict (topic_id, version) do update
    set answer_text = excluded.answer_text,
        access_class = excluded.access_class,
        effective_from = excluded.effective_from,
        effective_to = excluded.effective_to,
        source_note = excluded.source_note,
        is_active = excluded.is_active;

    update public.faq_entries
    set answer_text = 'I can connect you with a representative who can help start a claim. If you have your policy number and the incident date and location handy, those may help. Would you like me to connect you?'
    where topic_id = 'start_new_claim' and version = 'faq-v3';

    insert into public.faq_entries (
        topic_id, version, answer_text, access_class, effective_from,
        effective_to, source_note, is_active
    ) values (
        'general_enquiries_email', 'faq-v3',
        'The fictional general enquiries address for this demo is general.enquiries@observe-insurance.example. It is non-deliverable and not monitored. Do not email claim documents or personal, medical, or payment information. A representative can provide a verified contact or secure submission method.',
        'PUBLIC', date '2026-09-28', null,
        'Approved synthetic Observe Insurance FAQ v3', true
    )
    on conflict (topic_id, version) do update
    set answer_text = excluded.answer_text,
        access_class = excluded.access_class,
        effective_from = excluded.effective_from,
        effective_to = excluded.effective_to,
        source_note = excluded.source_note,
        is_active = excluded.is_active;
end $$;
