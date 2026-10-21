-- Separate cloud exam data model. This does not alter or migrate SQLite data.
-- Run only in the intended Supabase project after reviewing CLOUD_EXAMS.md.

create table if not exists public.cloud_exams (
    id uuid primary key default gen_random_uuid(),
    title text not null check (length(btrim(title)) between 1 and 160),
    status text not null default 'draft'
        check (status in ('draft', 'open', 'closed')),
    opens_at timestamptz not null,
    closes_at timestamptz not null,
    duration_minutes integer not null check (duration_minutes between 1 and 480),
    attempt_limit integer not null default 1 check (attempt_limit between 1 and 20),
    pass_marks integer not null default 40 check (pass_marks between 0 and 100),
    randomize_questions boolean not null default false,
    question_snapshot jsonb not null
        check (
            jsonb_typeof(question_snapshot) = 'array'
            and jsonb_array_length(question_snapshot) between 1 and 500
        ),
    results_published_at timestamptz,
    created_by text not null check (length(btrim(created_by)) between 1 and 120),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check (opens_at < closes_at),
    check (results_published_at is null or status = 'closed')
);

create table if not exists public.cloud_exam_candidates (
    id uuid primary key default gen_random_uuid(),
    exam_id uuid not null references public.cloud_exams(id) on delete restrict,
    candidate_name text not null check (length(btrim(candidate_name)) between 1 and 120),
    registration_number text not null
        check (length(btrim(registration_number)) between 1 and 128),
    registration_key text not null check (registration_key ~ '^[0-9a-f]{64}$'),
    access_code_salt text not null check (access_code_salt ~ '^[0-9a-f]{32}$'),
    access_code_hash text not null check (access_code_hash ~ '^[0-9a-f]{64}$'),
    is_active boolean not null default true,
    created_at timestamptz not null default now(),
    unique (exam_id, registration_key),
    unique (id, exam_id)
);

create table if not exists public.cloud_exam_attempts (
    id uuid primary key default gen_random_uuid(),
    exam_id uuid not null references public.cloud_exams(id) on delete restrict,
    candidate_id uuid not null,
    attempt_number integer not null check (attempt_number > 0),
    status text not null default 'in_progress'
        check (status in ('in_progress', 'submitted')),
    question_snapshot jsonb not null
        check (
            jsonb_typeof(question_snapshot) = 'array'
            and jsonb_array_length(question_snapshot) between 1 and 500
        ),
    answers jsonb not null default '[]'::jsonb
        check (jsonb_typeof(answers) = 'array'),
    started_at timestamptz not null default now(),
    deadline_at timestamptz not null,
    last_saved_at timestamptz not null default now(),
    submitted_at timestamptz,
    score integer check (score is null or score >= 0),
    total_questions integer check (total_questions is null or total_questions > 0),
    passed boolean,
    submission_reason text
        check (submission_reason is null or submission_reason in ('manual', 'time_expired')),
    created_at timestamptz not null default now(),
    foreign key (candidate_id, exam_id)
        references public.cloud_exam_candidates(id, exam_id) on delete restrict,
    unique (id, exam_id),
    unique (candidate_id, attempt_number),
    check (deadline_at > started_at),
    check (
        (status = 'in_progress' and submitted_at is null)
        or
        (status = 'submitted' and submitted_at is not null
            and score is not null and total_questions is not null and passed is not null)
    )
);

create unique index if not exists cloud_exam_one_active_attempt_per_candidate
    on public.cloud_exam_attempts(candidate_id)
    where status = 'in_progress';

create index if not exists cloud_exam_attempts_exam_status
    on public.cloud_exam_attempts(exam_id, status, started_at);

create table if not exists public.cloud_exam_proctoring_events (
    id bigint generated always as identity primary key,
    exam_id uuid not null references public.cloud_exams(id) on delete restrict,
    attempt_id uuid not null,
    event_type text not null check (length(btrim(event_type)) between 1 and 80),
    occurred_at timestamptz not null default now(),
    evidence_object_path text,
    review_status text not null default 'unreviewed'
        check (review_status in ('unreviewed', 'valid_alert', 'false_positive', 'needs_follow_up')),
    review_note text,
    reviewed_by text,
    reviewed_at timestamptz,
    foreign key (attempt_id, exam_id)
        references public.cloud_exam_attempts(id, exam_id) on delete restrict
);

create index if not exists cloud_exam_events_attempt_time
    on public.cloud_exam_proctoring_events(attempt_id, occurred_at desc);

create table if not exists public.cloud_exam_audit_log (
    id bigint generated always as identity primary key,
    exam_id uuid references public.cloud_exams(id) on delete restrict,
    actor text not null check (length(btrim(actor)) between 1 and 120),
    action text not null check (length(btrim(action)) between 1 and 80),
    details jsonb not null default '{}'::jsonb
        check (jsonb_typeof(details) = 'object'),
    created_at timestamptz not null default now()
);

create table if not exists public.cloud_exam_login_limits (
    key_hash text primary key check (key_hash ~ '^[0-9a-f]{64}$'),
    window_started_at timestamptz not null default now(),
    attempts integer not null default 0 check (attempts >= 0),
    updated_at timestamptz not null default now()
);

alter table public.cloud_exams enable row level security;
alter table public.cloud_exam_candidates enable row level security;
alter table public.cloud_exam_attempts enable row level security;
alter table public.cloud_exam_proctoring_events enable row level security;
alter table public.cloud_exam_audit_log enable row level security;
alter table public.cloud_exam_login_limits enable row level security;

revoke all on table
    public.cloud_exams,
    public.cloud_exam_candidates,
    public.cloud_exam_attempts,
    public.cloud_exam_proctoring_events,
    public.cloud_exam_audit_log,
    public.cloud_exam_login_limits
from public, anon, authenticated;

grant all on table
    public.cloud_exams,
    public.cloud_exam_candidates,
    public.cloud_exam_attempts,
    public.cloud_exam_proctoring_events,
    public.cloud_exam_audit_log,
    public.cloud_exam_login_limits
to service_role;

-- The Python service generates high-entropy invitation codes and stores only
-- their salted SHA-256 verifier. This RPC makes a roster import all-or-nothing.
create or replace function public.import_cloud_exam_roster(
    p_exam_id uuid,
    p_candidates jsonb,
    p_actor text
)
returns integer
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
    v_exam public.cloud_exams%rowtype;
    v_candidate jsonb;
    v_inserted integer := 0;
begin
    if length(btrim(coalesce(p_actor, ''))) = 0 then
        raise exception 'An administrator identity is required'
            using errcode = '22023';
    end if;

    if jsonb_typeof(p_candidates) is distinct from 'array'
       or jsonb_array_length(p_candidates) < 1
       or jsonb_array_length(p_candidates) > 5000 then
        raise exception 'Roster must contain between 1 and 5000 candidates'
            using errcode = '22023';
    end if;

    select * into v_exam
    from public.cloud_exams
    where id = p_exam_id
    for share;

    if not found then
        raise exception 'Exam not found' using errcode = 'P0002';
    end if;

    if v_exam.status <> 'draft' then
        raise exception 'Candidates can only be added to a draft exam'
            using errcode = '22023';
    end if;

    if exists (
        select 1
        from public.cloud_exam_attempts
        where exam_id = p_exam_id
    ) then
        raise exception 'A roster cannot be changed after an attempt has started'
            using errcode = '22023';
    end if;

    for v_candidate in select value from jsonb_array_elements(p_candidates)
    loop
        insert into public.cloud_exam_candidates (
            exam_id,
            candidate_name,
            registration_number,
            registration_key,
            access_code_salt,
            access_code_hash
        )
        values (
            p_exam_id,
            v_candidate ->> 'candidate_name',
            v_candidate ->> 'registration_number',
            v_candidate ->> 'registration_key',
            v_candidate ->> 'access_code_salt',
            v_candidate ->> 'access_code_hash'
        );
        v_inserted := v_inserted + 1;
    end loop;

    insert into public.cloud_exam_audit_log (exam_id, actor, action, details)
    values (
        p_exam_id,
        p_actor,
        'roster_imported',
        jsonb_build_object('candidate_count', v_inserted)
    );

    return v_inserted;
end;
$$;

create or replace function public.set_cloud_exam_status(
    p_exam_id uuid,
    p_status text,
    p_actor text
)
returns public.cloud_exams
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
    v_exam public.cloud_exams%rowtype;
begin
    if p_status is null
       or p_status not in ('open', 'closed')
       or length(btrim(coalesce(p_actor, ''))) = 0 then
        raise exception 'Invalid exam status or actor'
            using errcode = '22023';
    end if;

    select * into v_exam
    from public.cloud_exams
    where id = p_exam_id
    for update;

    if not found then
        raise exception 'Exam not found' using errcode = 'P0002';
    end if;

    if v_exam.results_published_at is not null and p_status <> 'closed' then
        raise exception 'A published exam cannot be reopened'
            using errcode = '22023';
    end if;

    if v_exam.status = 'closed' and p_status = 'open' then
        raise exception 'A closed exam cannot be reopened'
            using errcode = '22023';
    end if;

    if p_status = 'open'
       and v_exam.status = 'draft'
       and not exists (
           select 1 from public.cloud_exam_candidates
           where exam_id = p_exam_id and is_active
       ) then
        raise exception 'Add at least one active candidate before opening the exam'
            using errcode = 'P0001';
    end if;

    update public.cloud_exams
    set status = p_status, updated_at = now()
    where id = p_exam_id
    returning * into v_exam;

    insert into public.cloud_exam_audit_log (exam_id, actor, action, details)
    values (p_exam_id, p_actor, 'exam_status_changed', jsonb_build_object('status', p_status));

    return v_exam;
end;
$$;

create or replace function public.start_cloud_exam_attempt(
    p_exam_id uuid,
    p_candidate_id uuid,
    p_question_snapshot jsonb
)
returns setof public.cloud_exam_attempts
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
    v_exam public.cloud_exams%rowtype;
    v_candidate public.cloud_exam_candidates%rowtype;
    v_attempt public.cloud_exam_attempts%rowtype;
    v_attempt_count integer;
    v_initial_answers jsonb;
begin
    if jsonb_typeof(p_question_snapshot) is distinct from 'array'
       or jsonb_array_length(p_question_snapshot) < 1
       or jsonb_array_length(p_question_snapshot) > 500 then
        raise exception 'Invalid question snapshot' using errcode = '22023';
    end if;

    select * into v_exam
    from public.cloud_exams
    where id = p_exam_id
    for share;

    if not found then
        raise exception 'Exam not found' using errcode = 'P0002';
    end if;

    select * into v_candidate
    from public.cloud_exam_candidates
    where id = p_candidate_id and exam_id = p_exam_id and is_active
    for update;

    if not found then
        raise exception 'Candidate is not on this exam roster'
            using errcode = 'P0002';
    end if;

    select * into v_attempt
    from public.cloud_exam_attempts
    where candidate_id = p_candidate_id and status = 'in_progress'
    order by attempt_number desc
    limit 1;

    if found then
        return next v_attempt;
        return;
    end if;

    if v_exam.status <> 'open'
       or now() < v_exam.opens_at
       or now() >= v_exam.closes_at then
        raise exception 'This exam is not currently available'
            using errcode = 'P0001';
    end if;

    select count(*) into v_attempt_count
    from public.cloud_exam_attempts
    where candidate_id = p_candidate_id;

    if v_attempt_count >= v_exam.attempt_limit then
        raise exception 'Candidate attempt limit reached' using errcode = 'P0001';
    end if;

    select jsonb_agg('null'::jsonb order by answer_index)
    into v_initial_answers
    from generate_series(1, jsonb_array_length(p_question_snapshot)) as generated(answer_index);

    insert into public.cloud_exam_attempts (
        exam_id,
        candidate_id,
        attempt_number,
        question_snapshot,
        answers,
        deadline_at
    )
    values (
        p_exam_id,
        p_candidate_id,
        v_attempt_count + 1,
        p_question_snapshot,
        v_initial_answers,
        now() + make_interval(mins => v_exam.duration_minutes)
    )
    returning * into v_attempt;

    return next v_attempt;
end;
$$;

create or replace function public.calculate_cloud_exam_score(
    p_questions jsonb,
    p_answers jsonb
)
returns table(score integer, total integer)
language plpgsql
immutable
set search_path = pg_catalog, public
as $$
declare
    v_index integer;
    v_total integer;
    v_score integer := 0;
    v_question jsonb;
    v_answer jsonb;
    v_correct text;
    v_selected text;
begin
    if jsonb_typeof(p_questions) is distinct from 'array'
       or jsonb_typeof(p_answers) is distinct from 'array'
       or jsonb_array_length(p_questions) <> jsonb_array_length(p_answers) then
        raise exception 'Attempt answers do not match the question snapshot'
            using errcode = '22023';
    end if;

    v_total := jsonb_array_length(p_questions);
    if v_total < 1 or v_total > 500 then
        raise exception 'Invalid question count' using errcode = '22023';
    end if;

    for v_index in 0..v_total - 1 loop
        v_question := p_questions -> v_index;
        v_answer := p_answers -> v_index;
        v_correct := v_question ->> 'correct_index';

        if v_correct is null or v_correct !~ '^[0-3]$' then
            raise exception 'Invalid correct-answer snapshot'
                using errcode = '22023';
        end if;

        if jsonb_typeof(v_answer) = 'number' then
            v_selected := v_answer #>> '{}';
            if v_selected !~ '^[0-3]$' then
                raise exception 'Invalid answer value' using errcode = '22023';
            end if;
            if v_selected = v_correct then
                v_score := v_score + 1;
            end if;
        elsif v_answer <> 'null'::jsonb then
            raise exception 'Invalid answer value' using errcode = '22023';
        end if;
    end loop;

    return query select v_score, v_total;
end;
$$;

create or replace function public.save_cloud_exam_answers(
    p_exam_id uuid,
    p_candidate_id uuid,
    p_attempt_id uuid,
    p_answers jsonb
)
returns table(status text, last_saved_at timestamptz, deadline_at timestamptz)
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
    v_attempt public.cloud_exam_attempts%rowtype;
    v_score integer;
    v_total integer;
    v_now timestamptz := now();
begin
    select * into v_attempt
    from public.cloud_exam_attempts
    where id = p_attempt_id and exam_id = p_exam_id and candidate_id = p_candidate_id
    for update;

    if not found then
        raise exception 'Exam attempt not found' using errcode = 'P0002';
    end if;

    if v_attempt.status = 'submitted' then
        return query select v_attempt.status, v_attempt.last_saved_at, v_attempt.deadline_at;
        return;
    end if;

    if v_now >= v_attempt.deadline_at then
        select c.score, c.total into v_score, v_total
        from public.calculate_cloud_exam_score(v_attempt.question_snapshot, v_attempt.answers) c;

        update public.cloud_exam_attempts
        set status = 'submitted',
            submitted_at = v_now,
            score = v_score,
            total_questions = v_total,
            passed = (v_score::numeric * 100 >=
                (select e.pass_marks::numeric * v_total
                 from public.cloud_exams e where e.id = p_exam_id)),
            submission_reason = 'time_expired',
            last_saved_at = v_now
        where id = p_attempt_id
        returning * into v_attempt;

        return query select v_attempt.status, v_attempt.last_saved_at, v_attempt.deadline_at;
        return;
    end if;

    if jsonb_typeof(p_answers) is distinct from 'array'
       or jsonb_array_length(p_answers) <> jsonb_array_length(v_attempt.question_snapshot) then
        raise exception 'Invalid answer progress' using errcode = '22023';
    end if;

    for v_score in 0..jsonb_array_length(p_answers) - 1 loop
        if jsonb_typeof(p_answers -> v_score) not in ('number', 'null') then
            raise exception 'Invalid answer value' using errcode = '22023';
        end if;
        if jsonb_typeof(p_answers -> v_score) = 'number'
           and ((p_answers -> v_score) #>> '{}') !~ '^[0-3]$' then
            raise exception 'Invalid answer value' using errcode = '22023';
        end if;
    end loop;

    update public.cloud_exam_attempts
    set answers = p_answers, last_saved_at = v_now
    where id = p_attempt_id
    returning * into v_attempt;

    return query select v_attempt.status, v_attempt.last_saved_at, v_attempt.deadline_at;
end;
$$;

create or replace function public.submit_cloud_exam_attempt(
    p_exam_id uuid,
    p_candidate_id uuid,
    p_attempt_id uuid,
    p_answers jsonb,
    p_reason text
)
returns public.cloud_exam_attempts
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
    v_attempt public.cloud_exam_attempts%rowtype;
    v_answers jsonb;
    v_score integer;
    v_total integer;
    v_pass_marks integer;
    v_now timestamptz := now();
begin
    if p_reason is null or p_reason not in ('manual', 'time_expired') then
        raise exception 'Invalid submission reason' using errcode = '22023';
    end if;

    select * into v_attempt
    from public.cloud_exam_attempts
    where id = p_attempt_id and exam_id = p_exam_id and candidate_id = p_candidate_id
    for update;

    if not found then
        raise exception 'Exam attempt not found' using errcode = 'P0002';
    end if;

    if v_attempt.status = 'submitted' then
        return v_attempt;
    end if;

    if p_reason = 'time_expired' and v_now < v_attempt.deadline_at then
        raise exception 'Exam time has not expired' using errcode = 'P0001';
    end if;

    if v_now >= v_attempt.deadline_at then
        v_answers := v_attempt.answers;
        p_reason := 'time_expired';
    else
        v_answers := p_answers;
    end if;

    select c.score, c.total into v_score, v_total
    from public.calculate_cloud_exam_score(v_attempt.question_snapshot, v_answers) c;

    select pass_marks into v_pass_marks
    from public.cloud_exams where id = p_exam_id;

    update public.cloud_exam_attempts
    set answers = v_answers,
        status = 'submitted',
        submitted_at = v_now,
        last_saved_at = v_now,
        score = v_score,
        total_questions = v_total,
        passed = (v_score::numeric * 100 >= v_pass_marks::numeric * v_total),
        submission_reason = p_reason
    where id = p_attempt_id
    returning * into v_attempt;

    return v_attempt;
end;
$$;

create or replace function public.publish_cloud_exam_results(
    p_exam_id uuid,
    p_actor text
)
returns timestamptz
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
    v_exam public.cloud_exams%rowtype;
    v_attempt public.cloud_exam_attempts%rowtype;
    v_score integer;
    v_total integer;
    v_published_at timestamptz;
begin
    if length(btrim(coalesce(p_actor, ''))) = 0 then
        raise exception 'An administrator identity is required'
            using errcode = '22023';
    end if;

    select * into v_exam
    from public.cloud_exams
    where id = p_exam_id
    for update;

    if not found or v_exam.status <> 'closed' then
        raise exception 'Close the exam before publishing results'
            using errcode = 'P0001';
    end if;

    for v_attempt in
        select * from public.cloud_exam_attempts
        where exam_id = p_exam_id and status = 'in_progress'
        for update
    loop
        if v_attempt.deadline_at > now() then
            raise exception 'An exam attempt is still in progress'
                using errcode = 'P0001';
        end if;

        select c.score, c.total into v_score, v_total
        from public.calculate_cloud_exam_score(v_attempt.question_snapshot, v_attempt.answers) c;

        update public.cloud_exam_attempts
        set status = 'submitted',
            submitted_at = now(),
            last_saved_at = now(),
            score = v_score,
            total_questions = v_total,
            passed = (v_score::numeric * 100 >= v_exam.pass_marks::numeric * v_total),
            submission_reason = 'time_expired'
        where id = v_attempt.id;
    end loop;

    update public.cloud_exams
    set results_published_at = coalesce(results_published_at, now()),
        updated_at = now()
    where id = p_exam_id
    returning results_published_at into v_published_at;

    insert into public.cloud_exam_audit_log (exam_id, actor, action, details)
    values (p_exam_id, p_actor, 'results_published', '{}'::jsonb);

    return v_published_at;
end;
$$;

create or replace function public.consume_cloud_exam_login_attempt(
    p_key_hash text,
    p_max_attempts integer default 8,
    p_window_seconds integer default 600
)
returns boolean
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
    v_attempts integer;
    v_window_started_at timestamptz;
begin
    if p_key_hash is null or p_key_hash !~ '^[0-9a-f]{64}$'
       or p_max_attempts is null or p_max_attempts < 1 or p_max_attempts > 100
       or p_window_seconds is null or p_window_seconds < 1 or p_window_seconds > 86400 then
        raise exception 'Invalid login attempt limit parameters'
            using errcode = '22023';
    end if;

    insert into public.cloud_exam_login_limits(key_hash)
    values (p_key_hash)
    on conflict (key_hash) do nothing;

    update public.cloud_exam_login_limits
    set attempts = case
            when window_started_at <= now() - make_interval(secs => p_window_seconds)
                then 1
            when attempts < p_max_attempts then attempts + 1
            else attempts
        end,
        window_started_at = case
            when window_started_at <= now() - make_interval(secs => p_window_seconds)
                then now()
            else window_started_at
        end,
        updated_at = now()
    where key_hash = p_key_hash
    returning attempts, window_started_at
    into v_attempts, v_window_started_at;

    return v_attempts <= p_max_attempts;
end;
$$;

create or replace function public.list_cloud_exam_admin_summary(
    p_limit integer default 100
)
returns table (
    id uuid,
    title text,
    status text,
    opens_at timestamptz,
    closes_at timestamptz,
    duration_minutes integer,
    attempt_limit integer,
    pass_marks integer,
    randomize_questions boolean,
    results_published_at timestamptz,
    created_at timestamptz,
    candidate_count bigint,
    attempt_count bigint,
    submitted_count bigint
)
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
    select
        e.id,
        e.title,
        e.status,
        e.opens_at,
        e.closes_at,
        e.duration_minutes,
        e.attempt_limit,
        e.pass_marks,
        e.randomize_questions,
        e.results_published_at,
        e.created_at,
        (select count(*) from public.cloud_exam_candidates c where c.exam_id = e.id),
        (select count(*) from public.cloud_exam_attempts a where a.exam_id = e.id),
        (select count(*) from public.cloud_exam_attempts a
            where a.exam_id = e.id and a.status = 'submitted')
    from public.cloud_exams e
    order by e.created_at desc
    limit least(greatest(coalesce(p_limit, 100), 1), 200)
$$;

create or replace function public.get_cloud_exam_admin_detail(p_exam_id uuid)
returns jsonb
language plpgsql
stable
security definer
set search_path = pg_catalog, public
as $$
declare
    v_result jsonb;
begin
    select jsonb_build_object(
        'exam', to_jsonb(e) - 'question_snapshot',
        'candidate_total', (
            select count(*) from public.cloud_exam_candidates
            where exam_id = p_exam_id
        ),
        'attempt_total', (
            select count(*) from public.cloud_exam_attempts
            where exam_id = p_exam_id
        ),
        'candidates', coalesce((
            select jsonb_agg(to_jsonb(candidate_row))
            from (
                select id, candidate_name, registration_number, is_active, created_at
                from public.cloud_exam_candidates
                where exam_id = p_exam_id
                order by candidate_name, registration_number
                limit 100
            ) as candidate_row
        ), '[]'::jsonb),
        'attempts', coalesce((
            select jsonb_agg(to_jsonb(attempt_row))
            from (
                select a.id, a.candidate_id, c.candidate_name,
                    c.registration_number, a.attempt_number, a.status,
                    a.started_at, a.deadline_at, a.submitted_at,
                    a.score, a.total_questions, a.passed
                from public.cloud_exam_attempts a
                join public.cloud_exam_candidates c
                    on c.id = a.candidate_id and c.exam_id = a.exam_id
                where a.exam_id = p_exam_id
                order by a.started_at desc
                limit 100
            ) as attempt_row
        ), '[]'::jsonb)
    )
    into v_result
    from public.cloud_exams e
    where e.id = p_exam_id;

    if v_result is null then
        raise exception 'Exam not found' using errcode = 'P0002';
    end if;
    return v_result;
end;
$$;

create or replace function public.export_cloud_exam_results(p_exam_id uuid)
returns table (
    candidate_name text,
    registration_number text,
    attempt_number integer,
    status text,
    score integer,
    total_questions integer,
    passed boolean,
    started_at timestamptz,
    submitted_at timestamptz
)
language sql
stable
security definer
set search_path = pg_catalog, public
as $$
    select c.candidate_name, c.registration_number,
        a.attempt_number, a.status, a.score, a.total_questions,
        a.passed, a.started_at, a.submitted_at
    from public.cloud_exam_attempts a
    join public.cloud_exam_candidates c
        on c.id = a.candidate_id and c.exam_id = a.exam_id
    where a.exam_id = p_exam_id
    order by c.registration_number, a.attempt_number
$$;

create or replace function public.record_cloud_exam_proctoring_event(
    p_exam_id uuid,
    p_attempt_id uuid,
    p_event_type text
)
returns bigint
language plpgsql
security definer
set search_path = pg_catalog, public
as $$
declare
    v_event_id bigint;
begin
    if p_event_type is null
       or p_event_type not in ('no_face', 'multiple_faces') then
        raise exception 'Invalid proctoring event type' using errcode = '22023';
    end if;

    if not exists (
        select 1 from public.cloud_exam_attempts
        where id = p_attempt_id and exam_id = p_exam_id and status = 'in_progress'
    ) then
        raise exception 'Active exam attempt not found' using errcode = 'P0002';
    end if;

    insert into public.cloud_exam_proctoring_events(exam_id, attempt_id, event_type)
    values (p_exam_id, p_attempt_id, p_event_type)
    returning id into v_event_id;

    return v_event_id;
end;
$$;

revoke all on function public.import_cloud_exam_roster(uuid, jsonb, text) from public, anon, authenticated;
revoke all on function public.set_cloud_exam_status(uuid, text, text) from public, anon, authenticated;
revoke all on function public.start_cloud_exam_attempt(uuid, uuid, jsonb) from public, anon, authenticated;
revoke all on function public.calculate_cloud_exam_score(jsonb, jsonb) from public, anon, authenticated;
revoke all on function public.save_cloud_exam_answers(uuid, uuid, uuid, jsonb) from public, anon, authenticated;
revoke all on function public.submit_cloud_exam_attempt(uuid, uuid, uuid, jsonb, text) from public, anon, authenticated;
revoke all on function public.publish_cloud_exam_results(uuid, text) from public, anon, authenticated;
revoke all on function public.consume_cloud_exam_login_attempt(text, integer, integer) from public, anon, authenticated;
revoke all on function public.list_cloud_exam_admin_summary(integer) from public, anon, authenticated;
revoke all on function public.get_cloud_exam_admin_detail(uuid) from public, anon, authenticated;
revoke all on function public.export_cloud_exam_results(uuid) from public, anon, authenticated;
revoke all on function public.record_cloud_exam_proctoring_event(uuid, uuid, text) from public, anon, authenticated;

grant execute on function public.import_cloud_exam_roster(uuid, jsonb, text) to service_role;
grant execute on function public.set_cloud_exam_status(uuid, text, text) to service_role;
grant execute on function public.start_cloud_exam_attempt(uuid, uuid, jsonb) to service_role;
grant execute on function public.calculate_cloud_exam_score(jsonb, jsonb) to service_role;
grant execute on function public.save_cloud_exam_answers(uuid, uuid, uuid, jsonb) to service_role;
grant execute on function public.submit_cloud_exam_attempt(uuid, uuid, uuid, jsonb, text) to service_role;
grant execute on function public.publish_cloud_exam_results(uuid, text) to service_role;
grant execute on function public.consume_cloud_exam_login_attempt(text, integer, integer) to service_role;
grant execute on function public.list_cloud_exam_admin_summary(integer) to service_role;
grant execute on function public.get_cloud_exam_admin_detail(uuid) to service_role;
grant execute on function public.export_cloud_exam_results(uuid) to service_role;
grant execute on function public.record_cloud_exam_proctoring_event(uuid, uuid, text) to service_role;

insert into storage.buckets (id, name, public)
values ('cloud-exam-evidence', 'cloud-exam-evidence', false)
on conflict (id) do update set public = false;

notify pgrst, 'reload schema';
