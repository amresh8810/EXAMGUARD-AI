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
  compare against that saved face with FaceNet512 by default.
- For local evaluation only, `EXAMGUARD_FACE_MATCHER=sface` opts into YuNet/SFace.
  This mode requires an existing enrollment, does not create or upload face
  profiles, and uses an uncalibrated example threshold; it is not enabled in
  production.
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
  open/resumable assessment availability, available questions, and writable
  autosave storage.
- Keeps **Start Examination** disabled until all checks pass.
- Runs independent camera and server checks together, and reports assessment
  availability, questions, and autosave separately. The one-face diagnostic
  also runs independently of assessment availability.
- Normalizes local image contrast before the one-face check to reduce missed
  detections in uneven lighting, without relaxing the exactly-one-face rule.
- Keeps exam start blocked until every existing server and camera check passes.
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

- Saves submitted normal-exam answers and calculated score in the private
  Supabase `normal_exam_submissions` table.
- Shows the score, 50% pass/fail outcome, monitoring summary, and downloadable
  PDF only after an administrator publishes the result.
- After signing in again, a candidate can open their latest published normal
  exam result from the instructions page and download its PDF; both lookups are
  restricted to their verified registration number.
- Admins can inspect submitted answers and publish each normal-exam result in
  the administrator portal.
- Cloud Exams keep their separate result-publication workflow.

Main files:

- `frontend/app.py` (`/result`, `/download_report`, normal-exam admin routes,
  and evidence routes)
- `frontend/templates/result.html`
- `frontend/templates/admin_normal_exam_submissions.html`
- `backend/normal_exam_results.py`
- `database/normal_exam_submissions.sql`
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
- `frontend/templates/admin_normal_exam_submissions.html` (admin-only
  submission review and result publication)

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

## 9. Normal exam availability landing page

- Shows candidates whether an administrator has opened the normal exam and
  whether its question bank contains a valid assessment.
- Admins can manually open or close new normal-exam attempts from the admin
  portal. The initial setting is open to preserve existing access.
- Closed state is enforced by the candidate readiness checks and server-side
  exam-session creation; already-started attempts can still resume and submit.
- Sends candidates to the existing sign-in and face-verification flow when the
  assessment is open and valid; otherwise, it explains why the test is
  unavailable.
- This is a manual open/closed control, not a scheduled test calendar. The
  local database seeds default questions when its question table is empty.
- Keeps the sign-in page available at `/login`; Cloud Exam access remains a
  separate invite-based workflow.

Main files:

- `frontend/app.py` (candidate availability, exam-start gate, and admin routes)
- `backend/normal_exam_availability.py`
- `database/normal_exam_availability.sql`
- `frontend/templates/candidate_assessment.html`
- `frontend/templates/admin_normal_assessment.html`
- `frontend/static/css/candidate-assessment.css`
- `frontend/static/js/instructions.js` (preserves in-progress exam recovery)
- `frontend/templates/admin_portal.html` (admin navigation link)
- `frontend/templates/login.html`

## Shared files: ask before changing

These files serve multiple existing flows. A requested feature that requires
changing one of them needs the user's explicit approval first:

- `frontend/app.py`
- `backend/database.py`
- `backend/normal_exam_availability.py`
- `database/normal_exam_availability.sql`
- `frontend/templates/candidate_assessment.html`
- `frontend/templates/admin_normal_assessment.html`
- `frontend/static/css/candidate-assessment.css`
- `frontend/static/js/exam.js`
- `frontend/templates/exam.html`
- `frontend/templates/result.html`
- `frontend/static/js/instructions.js`
- `frontend/templates/instructions.html`
- `frontend/static/js/login.js`
- `frontend/templates/login.html`
- `backend/monitor_state.py`
- `frontend/templates/admin_portal.html`
- `frontend/static/css/admin.css`

This list is a starting point, not exhaustive. Check the implementation map
above before editing any other existing feature file.
