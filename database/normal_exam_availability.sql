-- Server-only availability control for the normal candidate exam.
-- Apply once to the Supabase project used by the app before deploying the code.

create table if not exists public.normal_exam_availability (
    assessment_key text primary key
        check (assessment_key = 'normal_exam'),
    is_open boolean not null default true,
    updated_at timestamptz not null default now(),
    updated_by text not null default 'System'
        check (length(btrim(updated_by)) between 1 and 120)
);

insert into public.normal_exam_availability (
    assessment_key,
    is_open,
    updated_at,
    updated_by
)
values ('normal_exam', true, now(), 'System')
on conflict (assessment_key) do nothing;

alter table public.normal_exam_availability enable row level security;
revoke all on table public.normal_exam_availability from anon, authenticated;
grant select, update on table public.normal_exam_availability to service_role;

notify pgrst, 'reload schema';
