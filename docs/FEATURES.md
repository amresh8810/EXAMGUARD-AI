# ExamGuard AI feature inventory

This file records the features currently implemented in the project and the
main files that implement each one. It is a change-safety map, not a promise
that every flow has been re-tested in every environment. Read it before making
code changes; follow the approval rules in
[`.github/copilot-instructions.md`](../.github/copilot-instructions.md).

## Existing features

### 1. Candidate sign-in and face identity verification

- Candidate enters a full name and registration number.
- The candidate grants browser camera access, previews the live feed, and
  captures a JPEG frame for server-side verification; the server does not try
  to open a webcam attached to the hosting machine.
- The submitted frame must contain exactly one face during verification.
- The first accepted face for a registration number is enrolled; later sign-ins
  compare against that saved face with FaceNet512.
- Candidate profile details and the face-image object path are stored using
  Supabase; the face image is held in the private `candidate-faces` bucket.
- Verification failures do not create an authenticated candidate session.
- Enrollment images are sensitive biometric data. A first-time self-entered
  registration number alone does not prove ownership; production use should
  pre-enroll candidates through a trusted administrator.

Main files:

- `frontend/templates/login.html`
- `frontend/static/js/login.js`
- `frontend/app.py` (`/verify` and face/profile helpers)
- `database/supabase_candidate_profiles.sql`
- `README.md` (Supabase setup and enrollment notes)

### 2. Pre-exam readiness gate

- Requires a valid candidate session, live browser camera, one visible face,
  available questions, and writable autosave storage.
- Keeps **Start Examination** disabled until all checks pass.
- Rechecks required conditions before starting and releases the readiness
  camera as the exam opens.

Main files:

- `frontend/templates/instructions.html`
- `frontend/static/js/instructions.js`
- `frontend/static/css/instructions.css`
- `frontend/app.py` (`/api/exam/readiness` and `/api/exam/readiness/face`)

### 3. Exam questions, progress saving, and recovery

- Presents a question snapshot for an exam attempt, with answer selection,
  question navigation, and review flags.
- Saves answers and exam progress to SQLite and keeps a browser-local recovery
  copy.
- Allows a candidate to resume an in-progress exam after refresh or sign-in
  session timeout using the saved exam session.
- Keeps the 60-minute deadline running while the candidate is away; saves the
  latest available progress and submits when the deadline expires.
- Retains exam recovery records for a limited period and avoids duplicate
  submission reports.

Main files:

- `frontend/templates/exam.html`
- `frontend/static/js/exam.js`
- `frontend/static/css/exam.css`
- `frontend/app.py` (exam-session, autosave, and submit routes)
- `backend/database.py` (exam-session persistence and reports)

### 4. Live exam monitoring and violation evidence

- Uses the webcam feed for candidate-presence and face-count monitoring.
- Records no-face and multiple-face events and monitors exam-tab visibility or
  focus changes.
- Collects evidence for recorded violations and keeps monitoring state scoped
  to the active exam attempt.
- Automatically submits the exam when the existing 25-violation limit is
  reached.

Main files:

- `frontend/app.py` (`/video_feed`, `/monitor`, violation, and evidence routes)
- `frontend/static/js/exam.js`
- `frontend/templates/exam.html`
- `backend/monitor_state.py`
- `backend/tab_switch.py`
- `backend/tab_switch_detector.py`

### 5. Results and downloadable reports

- Shows a candidate's exam result and recorded monitoring summary.
- Supports downloadable PDF reports and evidence review.

Main files:

- `frontend/app.py` (`/result`, `/download_report`, and evidence routes)
- `frontend/templates/result.html`
- `frontend/templates/evidence.html`
- `backend/database.py`

### 6. Protected administrator portal

- Provides environment-configured administrator sign-in and sign-out.
- Protects admin-only report/evidence access and question-management actions
  with authenticated sessions and request protections.
- Provides report/violation summaries, analytics, and question-bank
  management.

Main files:

- `frontend/templates/admin_login.html`
- `frontend/templates/admin_portal.html`
- `frontend/templates/admin.html`
- `frontend/static/css/admin.css`
- `frontend/static/js/admin.js`
- `frontend/app.py` (admin authentication, authorization, and API routes)
- `backend/database.py` (question and report storage)
- `.env.example` (configuration names; actual secrets belong only in ignored
  `.env`)

### 7. Admin report filtering and evidence review

- Filters completed report summaries by candidate, exam ID, and completion
  date; exports the filtered set as CSV or PDF.
- Shows newly recorded webcam evidence alongside its exam and candidate.
- Lets admins label evidence as a valid alert, false positive, needs follow-up,
  or return it to unreviewed, with an optional note and reviewer audit fields.
- Keeps older image files without reliable exam metadata in a separate
  unassigned historical-evidence section.

Main files:

- `frontend/templates/admin_reports.html`
- `frontend/templates/admin_evidence_review.html`
- `frontend/static/css/report-review.css`
- `frontend/static/js/report-review.js`
- `frontend/app.py` (admin report/export/review routes and additive evidence
  event registration)
- `backend/admin_reviews.py` (review ledger, filters, and review outcomes)
- `frontend/templates/admin_portal.html` (admin navigation link)

## 8. Separate cloud multi-candidate exam flow

This separately routed workflow supports cloud exam setup, one-time candidate
invitation codes, CSV/XLSX roster import, scheduled attempts, per-candidate
autosave, server-side scoring, periodic face-count monitoring, and
administrator-controlled result publication. The cloud database schema has
not yet been run against Supabase. Do not describe this as deployed or claim
200+ concurrent capacity until it is configured and load-tested.

The browser camera check detects face presence/count only. It does not compare
the candidate to an enrolled face profile or provide liveness verification.
This cloud workflow does not modify the legacy local exam or its records.

Main files:

- `database/cloud_exam_schema.sql` (separate Supabase tables, server-only RPCs,
  and private evidence bucket)
- `backend/cloud_exam_service.py` (question/roster validation, one-time
  invitation generation, and Supabase service methods)
- `backend/cloud_exam_routes.py` (admin/candidate routes, CSRF, camera checks,
  and result publication)
- `frontend/templates/admin_cloud_exams.html`
- `frontend/templates/admin_cloud_exam_detail.html`
- `frontend/templates/cloud_candidate_login.html`
- `frontend/templates/cloud_candidate_instructions.html`
- `frontend/templates/cloud_candidate_exam.html`
- `frontend/templates/cloud_candidate_result.html`
- `frontend/static/css/cloud-exams.css`
- `frontend/static/js/cloud-admin.js`
- `frontend/static/js/cloud-candidate.js`
- `frontend/app.py` (additive blueprint registration)
- `frontend/templates/admin_portal.html` (additive navigation link)
- `docs/CLOUD_EXAMS.md` (setup boundaries and roster format)

## Shared files: ask before changing

These files serve multiple existing flows. A requested feature that requires
changing one of them needs the user's explicit approval first:

- `frontend/app.py`
- `backend/database.py`
- `frontend/static/js/exam.js`
- `frontend/templates/exam.html`
- `frontend/static/js/instructions.js`
- `frontend/templates/instructions.html`
- `frontend/static/js/login.js`
- `frontend/templates/login.html`
- `backend/monitor_state.py`
- `frontend/templates/admin_portal.html`
- `frontend/static/css/admin.css`

This list is a starting point, not exhaustive. Check the implementation map
above before editing any other existing feature file.
