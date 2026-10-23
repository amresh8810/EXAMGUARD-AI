                                                                  ExamGuard AI

## AI-Powered Online Examination Proctoring System

ExamGuard AI is an intelligent online examination proctoring system designed to monitor candidates during remote assessments and detect suspicious activities in real time. The project combines Computer Vision, Artificial Intelligence, real-time monitoring, evidence collection, analytics, and automated reporting into a unified platform.

The system helps educational institutions and organizations conduct secure online examinations by automatically monitoring candidate behavior and generating evidence whenever suspicious activity is detected.

## Run locally on Windows

Use a virtual environment so the face-recognition dependencies install in a
short, project-local path:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe frontend\app.py
```

FaceNet model weights may download the first time an enrolled face is checked.

### Supabase candidate enrollment setup

The `candidate-faces` Storage bucket holds private face images; the
`candidate_profiles` table stores each candidate's name, registration number,
and image object path. In the Supabase SQL Editor, run
[`database/supabase_candidate_profiles.sql`](./database/supabase_candidate_profiles.sql)
once. Configure `SUPABASE_URL` and a server-side Supabase secret/service-role
key in the app's `.env`; do not expose that key in browser code. Candidate
passwords are not collected or stored by this login flow.

### Separate cloud exams for multiple candidates

The admin workspace has an additive **Cloud exams** page at
`/admin/cloud-exams`. It creates a separate cloud exam snapshot with a schedule,
duration, attempt limit, passing percentage, optional randomized question
order, and CSV/XLSX candidate roster. Each candidate receives a high-entropy
invitation code; only its salted verifier is stored, and the credential CSV is
downloaded only once after a successful import. Candidates use a separate
exam-specific access page, browser camera readiness check, cloud autosave, and
candidate-only published results.

Before using this flow, review
[`docs/CLOUD_EXAMS.md`](./docs/CLOUD_EXAMS.md), then explicitly apply
[`database/cloud_exam_schema.sql`](./database/cloud_exam_schema.sql) to the
intended Supabase project. Cloud setup requires the server-side `SUPABASE_URL`
and `SUPABASE_SECRET_KEY`; never expose the service-role key in browser code or
commit it. The schema is separate from local SQLite and does not copy existing
local candidates or exam records.

The cloud browser check detects face presence/count only; it does not perform
face identity matching. Use HTTPS for remote camera access. No deployment or
200+ concurrency guarantee is included: validate the intended hosting setup
and run realistic concurrent load tests before relying on this flow for a live
exam.

---

## Problem Statement

Online examinations have become increasingly popular, but maintaining examination integrity remains a major challenge. Common issues include:

- Candidate impersonation
- Multiple people appearing during the exam
- Candidates leaving the examination area
- Switching tabs to search for answers
- Lack of evidence for suspicious activities

ExamGuard AI addresses these challenges through automated AI-powered monitoring and evidence collection.

---

## Project Objectives

The primary objectives of this project are:

- Verify candidate identity before examination.
- Monitor candidates continuously during online exams.
- Detect suspicious behavior automatically.
- Capture evidence whenever violations occur.
- Store examination records securely.
- Provide administrators with a centralized dashboard.
- Generate downloadable reports for review and auditing.

---

## Key Features

### Candidate Verification System

Before starting an examination, candidates must complete identity verification.

Features:

- Browser-camera face enrollment on the first successful sign-in for a registration number
- Candidate previews the camera and submits a captured frame for server-side verification
- FaceNet512 comparison against that enrollment on subsequent sign-ins
- Fresh cloud enrollment when no trusted Supabase candidate profile exists; old
  local-only face references are replaced instead of treated as trusted
- Rejection when no face, multiple faces, or a different face is detected
- Prevention of unauthorized exam access

The first enrollment must be completed using the intended candidate's face.
Older profile photos are not used as trusted references because earlier versions
overwrote them on every sign-in. Enrollment images are cached under
`dataset/face_enrollments/` and synced to the private Supabase `candidate-faces`
bucket; candidate name, registration number, and image object path are stored
in `candidate_profiles`. These records should be treated as sensitive personal
and biometric data.
Because candidate names and registration numbers are self-entered, this flow
does not prove ownership of a registration number on its own; production
deployments should pre-enroll candidates through a trusted administrator.

---

### Pre-Exam Readiness Check

Before the exam can start, candidates can verify their signed-in session,
question bank, answer-autosave storage, live browser camera, and face detection.
The start button stays disabled until every check passes. Camera access must be
allowed in the browser; the readiness camera is released when the exam starts,
and the timer starts for a new exam or keeps its existing deadline when resuming.

---

### Real-Time AI Monitoring

The system continuously monitors candidates using their webcam.

Capabilities:

- Live video feed processing
- Face detection
- Candidate presence monitoring
- Real-time examination supervision

---

### Exam Auto-Save and Recovery

Candidate answers, review flags, and the current question are saved to the local
SQLite database as the candidate works and periodically in the background. A
browser-local recovery copy protects recent edits if the connection drops. After
a browser or login-session timeout, the same candidate can sign in again and
resume the saved exam. The 60-minute deadline continues while the candidate is
away; when it expires, the latest available progress is saved and the exam is
submitted automatically. In-progress and submitted recovery records are retained
for up to 30 days.

---

### No Face Detection

The system detects when the candidate leaves the camera view.

Detection Logic:

- No face visible for a specified duration
- Violation automatically recorded
- Evidence captured

Benefits:

- Prevents candidates from leaving during examinations
- Maintains examination integrity

---

### Multiple Face Detection

The system identifies when more than one person is present.

Detection Logic:

- Counts faces in each frame
- Triggers a violation if face count exceeds one

Benefits:

- Prevents collaboration during examinations
- Detects unauthorized assistance

---

### Tab Switching Detection

The system monitors whether the candidate leaves the examination tab.

Detection Logic:

- Browser visibility tracking
- Tab focus monitoring
- Automatic screenshot capture

Benefits:

- Prevents external searching during examinations
- Detects suspicious navigation behavior

---

### Evidence Collection System

Whenever a violation occurs, evidence is automatically generated.

Evidence Includes:

- Screenshots
- Captured frames
- Violation records

Purpose:

- Administrator review
- Report generation
- Audit trail creation

---

### Administrative Dashboard

The administrator dashboard provides centralized monitoring and analytics.

Features:

- Candidate report listing
- Search functionality
- Violation statistics
- Risk indicators
- Evidence viewer
- Interactive charts

Dashboard Components:

- Total Reports
- Total Violations
- AI Monitoring Status
- Violation Analytics Chart
- Risk Distribution Chart

---

### Admin Report Filtering and Evidence Review

At `/admin/reports`, administrators can filter completed report summaries by
candidate name, exam ID, and completion date, then export the matching set as
CSV or PDF. The evidence review queue is available from that page or at
`/admin/evidence-review`; admins can mark events as valid alerts, false
positives, or needing follow-up, add a note, and see who last reviewed them.

New webcam evidence is linked to its exam for review. Existing evidence files
without saved exam metadata are kept in a separate unassigned historical
section and are not attributed to a candidate or exam.

---

### Admin Sign-in and Configuration

The admin workspace is available at `/admin/login`. Configure a unique administrator
username and password in the project-root `.env` file; admin credentials are not
stored in source code. Generate a persistent Flask session key with:

```powershell
python -c "import secrets; print(secrets.token_hex(32))"
```

Set `FLASK_SECRET_KEY`, `ADMIN_USERNAME`, and `ADMIN_PASSWORD` in `.env`, then restart
the Flask app. Copy the variable names from `.env.example`; do not commit `.env`.
For HTTPS deployments, set `SESSION_COOKIE_SECURE=true`. Admin reports, evidence,
question changes, and sign-out require an authenticated admin session.

---

### Analytics Visualization

Interactive charts provide a visual representation of examination data.

Implemented Using:

- Chart.js

Charts:

- Violation Analytics Bar Chart
- Risk Distribution Doughnut Chart

Benefits:

- Quick insights
- Improved usability
- Better decision-making

---

### PDF Report Generation

The system generates downloadable PDF reports containing:

- Candidate information
- Face count
- Violation count
- Risk assessment
- Examination summary

Library Used:

- ReportLab

---

## System Workflow

```text
Candidate Login
       │
       ▼
Identity Verification
       │
       ▼
Exam Instructions
       │
       ▼
Start Examination
       │
       ▼
Live Monitoring
       │
       ▼
Violation Detection
       │
       ├── No Face Detection
       ├── Multiple Face Detection
       └── Tab Switch Detection
       │
       ▼
Evidence Collection
       │
       ▼
Database Storage
       │
       ▼
Admin Dashboard
       │
       ▼
PDF Report Generation
```

---

## Technology Stack

### Frontend

- HTML5
- CSS3
- JavaScript
- Chart.js

### Backend

- Python
- Flask

### Computer Vision

- OpenCV

### Database

- SQLite

### Reporting

- ReportLab

### Additional Libraries

- NumPy
- JSON
- OS

---

## Project Structure

```text
ExamGuardAI/
│
├── backend/
│   ├── database.py
│   ├── face_detection.py
│   ├── face_recognition.py
│   ├── exam_monitor.py
│   ├── verify_candidate.py
│   └── evidence/
│
├── frontend/
│   ├── templates/
│   │   ├── login.html
│   │   ├── instructions.html
│   │   ├── exam.html
│   │   ├── result.html
│   │   ├── admin.html
│   │   └── evidence.html
│   │
│   ├── static/
│   │   ├── css/
│   │   └── js/
│   │
│   └── app.py
│
├── database/
│
├── examguard.db
├── requirements.txt
└── README.md
```

---

## Detected Violations

| Violation Type | Description |
|---------------|-------------|
| No Face Detection | Candidate leaves webcam view |
| Multiple Face Detection | More than one person detected |
| Tab Switch Detection | Candidate exits examination tab |


---

## Screenshots

### Login Page
<img width="956" height="433" alt="image" src="https://github.com/user-attachments/assets/b6f65763-5d14-4ea0-9c4b-90314a73c6a7" />

### Instructions Page
<img width="529" height="431" alt="image" src="https://github.com/user-attachments/assets/ad3b8fca-90ca-46ba-b083-962885f22abf" />

### Live Examination Interface
<img width="1672" height="941" alt="image" src="https://github.com/user-attachments/assets/728a9a58-366c-42c6-bb49-3076e8edfcef" />
(ACTUAL IMAGES NOT ADDED DUE TO PRIVACY CONCERNS)

### Admin Dashboard
<img width="956" height="398" alt="image" src="https://github.com/user-attachments/assets/599e909f-a431-488f-b683-2b4719b0e2c2" />


### Evidence Viewer
<img width="950" height="153" alt="image" src="https://github.com/user-attachments/assets/43e9b9fd-13d6-49df-85f9-447ca6f9e6ab" />
(ACTUAL IMAGES NOT ADDED DUE TO PRIVACY CONCERNS)

---

## Future Enhancements

Potential future improvements include:

- Deep Learning based Face Recognition
- Eye Gaze Tracking
- Head Pose Estimation
- Mobile Phone Detection
- Audio Monitoring
- Cloud Deployment
- Multi-Candidate Analytics
- Real-Time Administrator Alerts
- AI-Based Cheating Probability Scoring

---

## Learning Outcomes

This project demonstrates practical implementation of:

- Computer Vision
- Artificial Intelligence Monitoring
- Flask Web Development
- Database Management
- Data Visualization
- Automated Report Generation
- Real-Time Detection Systems
- Full-Stack Application Development

---

## Conclusion

ExamGuard AI provides a complete AI-powered online examination monitoring solution capable of verifying candidate identity, monitoring behavior in real time, detecting suspicious activities, collecting evidence, generating reports, and providing administrators with actionable insights through an interactive dashboard.

The project demonstrates the integration of Artificial Intelligence, Computer Vision, Web Development, and Data Analytics to address real-world challenges in remote examinations.

---

## Author

### Amresh Kumar

**ExamGuard AI – AI-Powered Online Examination Proctoring System**

Built using Python, Flask, OpenCV, SQLite, ReportLab, HTML, CSS, JavaScript, and Chart.js.


## Privacy Notice

The facial dataset used during development and testing is intentionally excluded from this public repository for privacy reasons.

To test the application, users must create their own local dataset containing authorized candidate images.

No personal training images used during development are included in this repository.
