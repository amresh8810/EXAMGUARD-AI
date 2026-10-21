# Cloud multi-candidate exam foundation

This is a **separate cloud exam workflow**. The existing local SQLite exams,
sign-in, face verification, readiness check, monitoring, and saved records
remain unchanged.

## What is included

- A separate Supabase schema for cloud exams, rosters, attempts, proctoring
  events, review outcomes, and admin audit records:
  [`../database/cloud_exam_schema.sql`](../database/cloud_exam_schema.sql)
- A service module for validating exam/question data, parsing CSV and XLSX
  rosters, generating per-candidate invitation codes, and calling the
  server-side Supabase API:
  [`../backend/cloud_exam_service.py`](../backend/cloud_exam_service.py)
- Candidate invitation codes are generated on import; only a salted verifier
  is stored in the database. The credential CSV is returned once after a
  successful all-or-nothing roster import, and the admin UI provides a
  no-cache download for private distribution.
- Admins configure a per-candidate attempt limit. Candidates can start another
  attempt while the exam is open and they still have attempts remaining; the
  database enforces the limit.
- Result visibility is designed to be candidate-specific and remains hidden
  until an administrator explicitly publishes results.
- Tables and RPC functions deny access to `anon` and `authenticated` roles.
  The service must use a server-side Supabase secret/service-role key; it must
  never be sent to a browser.
- Candidate access routes rate-limit invalid invitation attempts and bind
  candidate APIs to a server-validated candidate/exam session. The cloud routes
  use an HttpOnly Flask session, CSRF tokens, and a database-backed
  invitation-attempt limit.
- Cloud candidates get a separate browser camera readiness check and periodic
  face-count monitoring. This detects face presence/count only; it does **not**
  compare against an enrolled identity or prove liveness.
- An admin-only cloud exam page is available at `/admin/cloud-exams` after
  Supabase is configured and the schema below has been applied.

## Not included or enabled yet

- The SQL has **not** been run against any Supabase project. Local SQLite data
  has not been migrated, copied, or changed by this feature.
- There is no hosting setup, HTTPS deployment, or load-test evidence. A local
  UI and schema do not establish support for 200+ simultaneous candidates.
- Cloud evidence is stored in a private bucket when an event is recorded.
  Cloud event review controls, evidence retention/cleanup, and independent
  load testing still need implementation.
- Candidate face images are not part of this schema. Cloud biometrics require
  a separate consent, enrollment, access, and retention design.

## Intended setup sequence

1. Choose and configure the intended Supabase project. Keep its service-role
   secret only in server environment configuration; never paste it into chat
   or commit it.
2. Review `database/cloud_exam_schema.sql` and the target project. Run it only
   after explicitly confirming that project. It creates new `cloud_*` tables,
   RPC functions, and a private evidence bucket; it does not migrate local
   data.
3. Add `FLASK_SECRET_KEY`, `SUPABASE_URL`, and the server-side
   `SUPABASE_SECRET_KEY` to the deployment environment. Set
   `SESSION_COOKIE_SECURE=true` on HTTPS; never expose the Supabase secret to
   browser code or commit it.
4. Test the roster, access-code download, candidate session, camera readiness,
   autosave, submission, proctor evidence, and result-publication workflows in
   a non-production project.
5. Choose HTTPS hosting and run realistic concurrent load tests before making
   any 200+ capacity claim.

## Roster format

Upload a UTF-8 `.csv` or `.xlsx` sheet with a header row containing
`candidate_name` (also accepts `full_name` or `name`) and
`registration_number` (also accepts `registration_no` or `reg_no`). Keep
registration numbers as text in spreadsheets to preserve leading zeroes.
Duplicate registration numbers are rejected before import. Legacy `.xls` files
are not supported.

## Current routes

- Admin exam list/create: `/admin/cloud-exams`
- Admin exam setup, roster import, status, attempt reports and publication:
  `/admin/cloud-exams/<exam-id>`
- Candidate access, camera readiness, exam, and published result:
  `/cloud-exams/<exam-id>`

An invitation-code CSV is downloaded only after a successful roster import.
Save it securely and distribute each code privately; it cannot be downloaded
again because only salted code verifiers are stored.
