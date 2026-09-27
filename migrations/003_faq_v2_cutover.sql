-- Atomically replace the active FAQ snapshot while preserving faq-v1 history.
-- Apply with no agent worker serving calls, then seed and verify faq-v2.

do $$
begin
    if (select count(*) from public.faq_entries where version = 'faq-v1')
       not in (0, 11) then
        raise exception 'faq-v1 is incomplete; refusing version cutover';
    end if;

    update public.faq_entries
    set is_active = false
    where version = 'faq-v1' and is_active;

    insert into public.faq_entries (
    topic_id, version, answer_text, access_class, effective_from,
    effective_to, source_note, is_active
    )
    select
    topic_id,
    'faq-v2',
    case topic_id
        when 'general_documents' then
            'Common examples may include photos, reports, estimates, and receipts. The exact list depends on the claim. After verification, I can check the documents listed for your claim or connect you with a representative.'
        when 'document_submission_general' then
            'Use only the submission method confirmed for your claim, and keep copies. After verification, I can check the instructions on your claim record. If no verified destination is listed, a representative can provide one.'
        else answer_text
    end,
    access_class,
    date '2026-09-27',
    null,
    'Approved synthetic Observe Insurance FAQ v2',
    true
    from public.faq_entries
    where version = 'faq-v1'
    on conflict (topic_id, version) do update
    set answer_text = excluded.answer_text,
        access_class = excluded.access_class,
        effective_from = excluded.effective_from,
        effective_to = excluded.effective_to,
        source_note = excluded.source_note,
        is_active = excluded.is_active;
end $$;
