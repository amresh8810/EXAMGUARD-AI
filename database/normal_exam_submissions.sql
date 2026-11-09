-- Durable, admin-reviewed results for the normal candidate exam flow.
-- Apply once to the Supabase project used by the app before deploying the code.

create table if not exists public.normal_exam_submissions (
    exam_id text primary key check (length(btrim(exam_id)) between 1 and 128),
    candidate_name text not null check (length(btrim(candidate_name)) between 1 and 120),
    reg_no text not null check (length(btrim(reg_no)) between 1 and 64),
    registration_key text not null check (registration_key ~ '^[0-9a-f]{64}$'),
    question_snapshot jsonb not null
        check (
            jsonb_typeof(question_snapshot) = 'array'
            and jsonb_array_length(question_snapshot) between 1 and 500
        ),
    answers jsonb not null
        check (jsonb_typeof(answers) = 'array'),
    score integer not null check (score >= 0),
    total_questions integer not null check (total_questions > 0),
    score_percentage numeric(5, 2) not null
        check (score_percentage between 0 and 100),
    passing_percentage smallint not null default 50
        check (passing_percentage between 1 and 100),
    passed boolean not null,
    result_status text not null default 'pending'
        check (result_status in ('pending', 'published')),
    submitted_at timestamptz not null default now(),
    published_at timestamptz,
    published_by text,
    face_count integer not null default 0 check (face_count >= 0),
    violations integer not null default 0 check (violations >= 0),
    evidence_count integer not null default 0 check (evidence_count >= 0),
    check (score <= total_questions),
    check (passed = (score * 100 >= total_questions * passing_percentage)),
    check (
        (result_status = 'pending' and published_at is null and published_by is null)
        or
        (result_status = 'published' and published_at is not null
            and length(btrim(published_by)) between 1 and 120)
    )
);

create index if not exists normal_exam_submissions_status_time
    on public.normal_exam_submissions(result_status, submitted_at desc);

create index if not exists normal_exam_submissions_candidate_time
    on public.normal_exam_submissions(registration_key, submitted_at desc);

alter table public.normal_exam_submissions enable row level security;
revoke all on table public.normal_exam_submissions from anon, authenticated;
grant select, insert, update on table public.normal_exam_submissions to service_role;

notify pgrst, 'reload schema';
