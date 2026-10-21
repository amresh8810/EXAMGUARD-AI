create table if not exists public.candidate_profiles (
    registration_key text primary key
        check (registration_key ~ '^[0-9a-f]{64}$'),
    candidate_name text not null,
    reg_no text not null,
    face_image_path text not null unique,
    created_at timestamptz not null default now()
);

alter table public.candidate_profiles enable row level security;
revoke all on table public.candidate_profiles from anon, authenticated;
grant select, insert on table public.candidate_profiles to service_role;

notify pgrst, 'reload schema';
