"""Offline regression checks for the ExamGuard AI feature boundaries."""

import csv
import io
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

_SUPABASE_ENV = {
    "SUPABASE_URL": "",
    "SUPABASE_KEY": "",
    "SUPABASE_SECRET_KEY": "",
    "SUPABASE_PUBLISHABLE_KEY": "",
}
_BOOTSTRAP_DIRECTORY = tempfile.TemporaryDirectory(prefix="examguard-tests-")

with (
    mock.patch.dict(
        os.environ,
        {**_SUPABASE_ENV, "FLASK_SECRET_KEY": "test-only-session-key"},
    ),
    mock.patch.object(dotenv, "load_dotenv"),
):
    from backend import database

    database.DB_NAME = str(Path(_BOOTSTRAP_DIRECTORY.name) / "bootstrap.sqlite3")

    from backend import admin_reviews, normal_exam_availability
    from backend.cloud_exam_service import (
        candidate_attempt_payload,
        prepare_question_snapshot,
        prepare_roster_upload,
        verify_access_code,
    )
    from backend.normal_exam_availability import NormalExamAvailabilityError
    from backend.normal_exam_results import (
        NormalExamResultError,
        calculate_result,
        get_latest_published_submission,
    )
    from frontend import app as exam_app


exam_app.app.config.update(TESTING=True, SECRET_KEY="examguard-test-session-key")
database.supabase = None
exam_app.supabase = None


class FeatureRegressionTests(unittest.TestCase):
    def setUp(self):
        self.database_directory = tempfile.TemporaryDirectory(
            prefix="examguard-feature-test-"
        )
        self.addCleanup(self.database_directory.cleanup)
        database_path = str(
            Path(self.database_directory.name) / "examguard-test.sqlite3"
        )
        for module in (database, admin_reviews, normal_exam_availability, exam_app):
            module.DB_NAME = database_path

        database.init_db()
        admin_reviews.init_admin_review_db()
        database.supabase = None
        exam_app.supabase = None
        exam_app.app.extensions.pop("cloud_exam_store", None)
        self.environment = mock.patch.dict(
            os.environ,
            {
                "ADMIN_USERNAME": "test-admin",
                "ADMIN_PASSWORD": "test-only-password",
                **_SUPABASE_ENV,
            },
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.client = exam_app.app.test_client()

    def sign_in_candidate(self, name="Test Candidate", reg_no="TEST-001"):
        with self.client.session_transaction() as candidate_session:
            candidate_session["candidate_name"] = name
            candidate_session["reg_no"] = reg_no

    def sign_in_admin(self):
        with self.client.session_transaction() as admin_session:
            admin_session["is_admin"] = True
            admin_session["admin_username"] = "test-admin"
            admin_session["admin_csrf_token"] = "test-csrf-token"

    def create_exam(self, exam_id="test-exam", resume_token="test-resume-token"):
        questions = exam_app._prepare_exam_questions(database.get_all_questions())
        database.create_exam_session(
            exam_id,
            exam_app._hash_resume_token(resume_token),
            "Test Candidate",
            "TEST-001",
            questions,
            duration_seconds=3600,
        )
        return questions, resume_token

    def test_candidate_home_and_login_routes_remain_available(self):
        home = self.client.get("/")
        self.assertEqual(home.status_code, 200)
        self.assertIn(b"Assessment available", home.data)
        self.assertIn(b"Start test", home.data)

        login = self.client.get("/login")
        self.assertEqual(login.status_code, 200)
        self.assertIn(b"Candidate Login", login.data)

    def test_candidate_face_login_requires_a_camera_frame(self):
        self.sign_in_candidate()
        response = self.client.post(
            "/verify",
            json={"name": "Test Candidate", "reg_no": "TEST-001"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("capture a frame", response.get_json()["message"])
        with self.client.session_transaction() as candidate_session:
            self.assertNotIn("candidate_name", candidate_session)

    def test_readiness_checks_candidate_session_questions_and_local_autosave(self):
        unauthenticated = self.client.post("/api/exam/readiness", json={})
        self.assertEqual(unauthenticated.status_code, 401)

        self.sign_in_candidate()
        response = self.client.post("/api/exam/readiness", json={})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "ready")
        self.assertTrue(response.get_json()["checks"]["availability"])
        self.assertTrue(response.get_json()["checks"]["autosave"])
        self.assertGreater(response.get_json()["checks"]["question_count"], 0)

    def test_instructions_page_renders_every_readiness_check(self):
        response = self.client.get("/instructions")
        self.assertEqual(response.status_code, 200)
        for check_id in (
            b"candidateSessionCheck",
            b"availabilityCheck",
            b"cameraCheck",
            b"faceCheck",
            b"questionsCheck",
            b"autosaveCheck",
        ):
            self.assertIn(check_id, response.data)
        self.assertIn(b"/static/js/instructions.js?v=5", response.data)

    def test_readiness_reports_question_failure_and_still_checks_autosave(self):
        self.sign_in_candidate()
        with (
            mock.patch.object(
                exam_app,
                "get_availability",
                return_value={"is_open": True},
            ),
            mock.patch.object(
                exam_app,
                "get_all_questions",
                side_effect=RuntimeError("test question storage failure"),
            ),
            self.assertLogs(exam_app.app.logger, level="ERROR"),
        ):
            response = self.client.post("/api/exam/readiness", json={})

        payload = response.get_json()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(payload["check"], "questions")
        self.assertTrue(payload["checks"]["availability"])
        self.assertFalse(payload["checks"]["questions"])
        self.assertTrue(payload["checks"]["autosave"])
        self.assertIn("questions", payload["errors"])

    def test_readiness_reports_autosave_failure_after_other_checks(self):
        self.sign_in_candidate()
        questions = database.get_all_questions()
        with (
            mock.patch.object(
                exam_app,
                "get_availability",
                return_value={"is_open": True},
            ),
            mock.patch.object(
                exam_app,
                "get_all_questions",
                return_value=questions,
            ),
            mock.patch.object(
                sqlite3,
                "connect",
                side_effect=sqlite3.OperationalError(
                    "test autosave storage failure"
                ),
            ),
            self.assertLogs(exam_app.app.logger, level="ERROR"),
        ):
            response = self.client.post("/api/exam/readiness", json={})

        payload = response.get_json()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(payload["check"], "autosave")
        self.assertTrue(payload["checks"]["availability"])
        self.assertTrue(payload["checks"]["questions"])
        self.assertFalse(payload["checks"]["autosave"])
        self.assertIn("autosave", payload["errors"])

    def test_face_readiness_runs_when_assessment_availability_is_unavailable(self):
        unauthenticated = self.client.post(
            "/api/exam/readiness/face",
            data={
                "frame": (io.BytesIO(b"jpeg-frame"), "readiness.jpg", "image/jpeg"),
            },
        )
        self.assertEqual(unauthenticated.status_code, 401)

        self.sign_in_candidate()
        cascade = mock.Mock()
        cascade.empty.return_value = False
        cascade.detectMultiScale.return_value = [(10, 10, 80, 80)]
        gray = object()
        normalized_gray = object()
        contrast_normalizer = mock.Mock()
        contrast_normalizer.apply.return_value = normalized_gray

        with (
            mock.patch.object(
                exam_app,
                "get_availability",
                side_effect=NormalExamAvailabilityError("test storage failure"),
            ) as availability_check,
            mock.patch.object(exam_app.cv2, "imdecode", return_value=object()),
            mock.patch.object(exam_app.cv2, "cvtColor", return_value=gray),
            mock.patch.object(
                exam_app.cv2,
                "createCLAHE",
                return_value=contrast_normalizer,
            ) as create_clahe,
            mock.patch.object(
                exam_app.cv2,
                "CascadeClassifier",
                return_value=cascade,
            ),
        ):
            blocked_start_check = self.client.post(
                "/api/exam/readiness",
                json={},
            )
            face_check = self.client.post(
                "/api/exam/readiness/face",
                data={
                    "frame": (io.BytesIO(b"jpeg-frame"), "readiness.jpg", "image/jpeg"),
                },
            )

        self.assertEqual(blocked_start_check.status_code, 503)
        self.assertEqual(blocked_start_check.get_json()["check"], "availability")
        self.assertFalse(blocked_start_check.get_json()["checks"]["availability"])
        self.assertTrue(blocked_start_check.get_json()["checks"]["candidate_session"])
        self.assertTrue(blocked_start_check.get_json()["checks"]["questions"])
        self.assertTrue(blocked_start_check.get_json()["checks"]["autosave"])
        self.assertIn("availability", blocked_start_check.get_json()["errors"])
        self.assertEqual(face_check.status_code, 200)
        self.assertEqual(face_check.get_json()["faces"], 1)
        self.assertEqual(availability_check.call_count, 1)
        create_clahe.assert_called_once_with(
            clipLimit=2.0,
            tileGridSize=(8, 8),
        )
        contrast_normalizer.apply.assert_called_once_with(gray)
        cascade.detectMultiScale.assert_called_once_with(
            normalized_gray,
            scaleFactor=1.2,
            minNeighbors=4,
            minSize=(60, 60),
        )

    def test_face_readiness_preserves_zero_one_and_multiple_face_counts(self):
        self.sign_in_candidate()
        gray = object()
        normalized_gray = object()
        face_results = (
            ([], 0),
            ([(10, 10, 80, 80)], 1),
            ([(10, 10, 80, 80), (100, 10, 80, 80)], 2),
        )

        for detected_faces, expected_count in face_results:
            with self.subTest(expected_count=expected_count):
                cascade = mock.Mock()
                cascade.empty.return_value = False
                cascade.detectMultiScale.return_value = detected_faces
                contrast_normalizer = mock.Mock()
                contrast_normalizer.apply.return_value = normalized_gray

                with (
                    mock.patch.object(
                        exam_app.cv2,
                        "imdecode",
                        return_value=object(),
                    ),
                    mock.patch.object(
                        exam_app.cv2,
                        "cvtColor",
                        return_value=gray,
                    ),
                    mock.patch.object(
                        exam_app.cv2,
                        "createCLAHE",
                        return_value=contrast_normalizer,
                    ),
                    mock.patch.object(
                        exam_app.cv2,
                        "CascadeClassifier",
                        return_value=cascade,
                    ),
                ):
                    response = self.client.post(
                        "/api/exam/readiness/face",
                        data={
                            "frame": (
                                io.BytesIO(b"jpeg-frame"),
                                "readiness.jpg",
                                "image/jpeg",
                            ),
                        },
                    )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.get_json()["faces"], expected_count)

    def test_closed_normal_exam_blocks_new_attempts_but_allows_resume(self):
        normal_exam_availability.set_availability(None, False, "test-admin")
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertIn(b"Assessment closed", self.client.get("/").data)
        self.sign_in_candidate()

        blocked = self.client.post("/api/exam/session", json={})
        self.assertEqual(blocked.status_code, 423)
        blocked_readiness = self.client.post("/api/exam/readiness", json={})
        self.assertEqual(blocked_readiness.status_code, 423)
        self.assertFalse(blocked_readiness.get_json()["checks"]["availability"])
        self.assertTrue(blocked_readiness.get_json()["checks"]["questions"])
        self.assertTrue(blocked_readiness.get_json()["checks"]["autosave"])

        _, resume_token = self.create_exam()
        with mock.patch.object(exam_app, "_ensure_exam_monitoring_stats"):
            resumed = self.client.post(
                "/api/exam/session",
                json={"resume_token": resume_token},
            )
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(resumed.get_json()["status"], "in_progress")

        readiness = self.client.post(
            "/api/exam/readiness",
            json={"resume_token": resume_token},
        )
        self.assertEqual(readiness.status_code, 200)
        self.assertTrue(readiness.get_json()["checks"]["availability"])

    def test_exam_snapshot_hides_answer_key_and_autosave_persists_progress(self):
        self.sign_in_candidate()
        with (
            mock.patch.object(exam_app, "_reset_exam_monitoring_stats"),
            mock.patch.object(exam_app, "_ensure_exam_monitoring_stats"),
        ):
            started = self.client.post("/api/exam/session", json={})
            self.assertEqual(started.status_code, 200)
            session_data = started.get_json()
            self.assertNotIn("correct", session_data["questions"][0])

            question_ids = [str(question["id"]) for question in session_data["questions"]]
            answers = [1] + [None] * (len(question_ids) - 1)
            progress = self.client.post(
                "/api/exam/autosave",
                json={
                    "resume_token": session_data["resume_token"],
                    "question_ids": question_ids,
                    "answers": answers,
                    "reviewed": [False] * len(question_ids),
                    "visited": [True] + [False] * (len(question_ids) - 1),
                    "current_question": 0,
                },
            )

        self.assertEqual(progress.status_code, 200)
        self.assertEqual(progress.get_json()["status"], "saved")
        stored = database.get_exam_session(session_data["exam_id"])
        self.assertEqual(stored["answers"], answers)

    def test_submitted_result_stays_private_until_published(self):
        questions, _ = self.create_exam(exam_id="result-exam")
        database.complete_exam_session(
            "result-exam",
            [0] * len(questions),
            [False] * len(questions),
            [True] + [False] * (len(questions) - 1),
            0,
            1,
            0,
            0,
        )
        self.sign_in_candidate()
        with self.client.session_transaction() as candidate_session:
            candidate_session["active_exam_id"] = "result-exam"

        pending = {
            "candidate_name": "Test Candidate",
            "result_status": "pending",
            "score": len(questions),
            "total_questions": len(questions),
            "score_percentage": 100,
            "passed": True,
            "face_count": 1,
            "violations": 0,
            "evidence_count": 0,
        }
        with mock.patch.object(exam_app, "get_submission", return_value=pending):
            hidden = self.client.get("/result")
        self.assertEqual(hidden.status_code, 200)
        self.assertIn(b"awaiting administrator publication", hidden.data)
        self.assertNotIn(
            f"{len(questions)} / {len(questions)}".encode(),
            hidden.data,
        )

        published = {**pending, "result_status": "published"}
        with mock.patch.object(exam_app, "get_submission", return_value=published):
            visible = self.client.get("/result")
        self.assertEqual(visible.status_code, 200)
        self.assertIn(
            f"{len(questions)} / {len(questions)}".encode(),
            visible.data,
        )

    def test_candidate_can_view_own_published_result_after_signing_in_again(self):
        self.sign_in_candidate(reg_no="TEST-001")
        published = {
            "exam_id": "published-exam",
            "candidate_name": "Test Candidate",
            "result_status": "published",
            "score": 8,
            "total_questions": 10,
            "score_percentage": 80,
            "passed": True,
            "face_count": 1,
            "violations": 0,
            "evidence_count": 0,
        }
        registration_key = exam_app._candidate_registration_key("TEST-001")
        with mock.patch.object(
            exam_app,
            "get_latest_published_submission",
            return_value=published,
        ) as lookup:
            response = self.client.get("/candidate/published-result")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Exam Result Published", response.data)
        self.assertIn(b"8 / 10", response.data)
        self.assertIn(b"/download_report?exam_id=published-exam", response.data)
        lookup.assert_called_once_with(exam_app.supabase, registration_key)

    def test_candidate_published_result_does_not_expose_other_candidate_results(self):
        self.sign_in_candidate(reg_no="TEST-001")
        with mock.patch.object(
            exam_app,
            "get_latest_published_submission",
            return_value=None,
        ) as lookup:
            response = self.client.get("/candidate/published-result")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"No result has been published for this candidate yet", response.data)
        lookup.assert_called_once_with(
            exam_app.supabase,
            exam_app._candidate_registration_key("TEST-001"),
        )

    def test_published_result_page_requires_a_verified_candidate_session(self):
        response = self.client.get("/candidate/published-result")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_candidate_can_download_published_report_after_signing_in_again(self):
        self.sign_in_candidate(reg_no="TEST-001")
        published = {
            "exam_id": "published-exam",
            "candidate_name": "Test Candidate",
            "result_status": "published",
            "score": 8,
            "total_questions": 10,
            "score_percentage": 80,
            "passed": True,
            "face_count": 1,
            "violations": 0,
            "evidence_count": 0,
        }
        registration_key = exam_app._candidate_registration_key("TEST-001")
        with (
            mock.patch.object(
                exam_app,
                "get_submission",
                return_value=published,
            ) as lookup,
            mock.patch.object(exam_app, "SimpleDocTemplate") as pdf_document,
            mock.patch.object(
                exam_app,
                "send_file",
                return_value=exam_app.Response(b"test pdf"),
            ),
        ):
            pdf_document.return_value.build.return_value = None
            response = self.client.get(
                "/download_report?exam_id=published-exam"
            )

        self.assertEqual(response.status_code, 200)
        lookup.assert_called_once_with(
            exam_app.supabase,
            "published-exam",
            registration_key,
        )

    def test_candidate_cannot_download_another_registration_number_report(self):
        unauthenticated = self.client.get(
            "/download_report?exam_id=another-candidate-exam"
        )
        self.assertEqual(unauthenticated.status_code, 302)
        self.assertIn("/login", unauthenticated.headers["Location"])

        self.sign_in_candidate(reg_no="TEST-001")
        with mock.patch.object(
            exam_app,
            "get_submission",
            return_value=None,
        ) as lookup:
            response = self.client.get(
                "/download_report?exam_id=another-candidate-exam"
            )

        self.assertEqual(response.status_code, 404)
        lookup.assert_called_once_with(
            exam_app.supabase,
            "another-candidate-exam",
            exam_app._candidate_registration_key("TEST-001"),
        )

    def test_normal_exam_scoring_and_invalid_snapshots(self):
        questions = [{"correct": 1}, {"correct": 2}]
        result = calculate_result(questions, [1, None])
        self.assertEqual(result["score"], 1)
        self.assertEqual(result["score_percentage"], 50)
        self.assertTrue(result["passed"])

        with self.assertRaises(NormalExamResultError):
            calculate_result([{"correct": True}], [0])

    def test_supabase_availability_read_and_write_use_the_single_setting(self):
        open_record = {
            "assessment_key": "normal_exam",
            "is_open": True,
            "updated_at": "2026-01-01T00:00:00+00:00",
            "updated_by": "System",
        }
        closed_record = {
            **open_record,
            "is_open": False,
            "updated_by": "test-admin",
        }
        query = mock.Mock()
        query.select.return_value = query
        query.eq.return_value = query
        query.limit.return_value = query
        query.update.return_value = query
        query.execute.side_effect = [
            SimpleNamespace(data=[open_record]),
            SimpleNamespace(data=[closed_record]),
        ]
        client = mock.Mock()
        client.table.return_value = query

        self.assertTrue(normal_exam_availability.get_availability(client)["is_open"])
        updated = normal_exam_availability.set_availability(
            client,
            False,
            "test-admin",
        )
        self.assertFalse(updated["is_open"])
        self.assertEqual(client.table.call_args.args[0], "normal_exam_availability")

    def test_latest_published_result_query_is_scoped_and_ordered(self):
        registration_key = "a" * 64
        published = {"exam_id": "published-exam", "result_status": "published"}
        query = mock.Mock()
        query.select.return_value = query
        query.eq.return_value = query
        query.order.return_value = query
        query.limit.return_value = query
        query.execute.return_value = SimpleNamespace(data=[published])
        client = mock.Mock()
        client.table.return_value = query

        result = get_latest_published_submission(client, registration_key)

        self.assertEqual(result, published)
        self.assertEqual(client.table.call_args.args[0], "normal_exam_submissions")
        self.assertEqual(
            query.eq.call_args_list,
            [
                mock.call("registration_key", registration_key),
                mock.call("result_status", "published"),
            ],
        )
        query.order.assert_called_once_with("submitted_at", desc=True)
        query.limit.assert_called_once_with(1)

        with self.assertRaises(NormalExamResultError):
            get_latest_published_submission(client, "invalid-registration-key")

    def test_admin_login_csrf_and_logout(self):
        rejected = self.client.post(
            "/admin/login",
            data={"username": "wrong", "password": "wrong"},
        )
        self.assertEqual(rejected.status_code, 200)
        self.assertIn(b"username or password is incorrect", rejected.data)

        accepted = self.client.post(
            "/admin/login",
            data={"username": "test-admin", "password": "test-only-password"},
        )
        self.assertEqual(accepted.status_code, 302)
        self.assertEqual(self.client.get("/admin").status_code, 200)

        missing_csrf = self.client.post("/admin/logout")
        self.assertEqual(missing_csrf.status_code, 400)
        with self.client.session_transaction() as admin_session:
            csrf_token = admin_session["admin_csrf_token"]
        logged_out = self.client.post(
            "/admin/logout",
            data={"csrf_token": csrf_token},
        )
        self.assertEqual(logged_out.status_code, 302)
        self.assertEqual(self.client.get("/admin").status_code, 302)

    def test_admin_can_change_normal_assessment_availability(self):
        self.sign_in_admin()
        page = self.client.get("/admin/normal-assessment")
        self.assertEqual(page.status_code, 200)

        closed = self.client.post(
            "/admin/normal-assessment/status",
            data={"is_open": "false", "csrf_token": "test-csrf-token"},
        )
        self.assertEqual(closed.status_code, 302)
        self.assertIn(b"Assessment closed", self.client.get("/").data)
        self.assertEqual(self.client.get("/login").status_code, 200)

        opened = self.client.post(
            "/admin/normal-assessment/status",
            data={"is_open": "true", "csrf_token": "test-csrf-token"},
        )
        self.assertEqual(opened.status_code, 302)
        self.assertIn(b"Assessment available", self.client.get("/").data)

    def test_admin_submission_route_is_protected_and_reports_storage_errors(self):
        unauthorized = self.client.get("/admin/submissions")
        self.assertEqual(unauthorized.status_code, 302)
        self.assertIn("/admin/login", unauthorized.headers["Location"])

        self.sign_in_admin()
        with self.assertLogs(exam_app.app.logger, level="ERROR"):
            unavailable = self.client.get("/admin/submissions")
        self.assertEqual(unavailable.status_code, 503)
        self.assertNotEqual(unavailable.status_code, 500)

        with mock.patch.object(exam_app, "list_submissions", return_value=[]):
            page = self.client.get("/admin/submissions")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"No normal exam submissions yet", page.data)

        rejected_publish = self.client.post(
            "/admin/submissions/test-exam/publish",
        )
        self.assertEqual(rejected_publish.status_code, 400)
        with mock.patch.object(exam_app, "publish_submission") as publish:
            published = self.client.post(
                "/admin/submissions/test-exam/publish",
                data={"csrf_token": "test-csrf-token"},
            )
        self.assertEqual(published.status_code, 302)
        publish.assert_called_once()

    def test_admin_question_management_requires_csrf_and_keeps_answer_key_private(self):
        question = {
            "question": "Which option is correct?",
            "option_a": "A",
            "option_b": "B",
            "option_c": "C",
            "option_d": "D",
            "correct": "1",
        }
        unauthorized = self.client.post("/api/questions/add", json=question)
        self.assertEqual(unauthorized.status_code, 401)

        self.sign_in_admin()
        missing_csrf = self.client.post("/api/questions/add", json=question)
        self.assertEqual(missing_csrf.status_code, 400)
        created = self.client.post(
            "/api/questions/add",
            json={**question, "csrf_token": "test-csrf-token"},
            headers={"X-CSRF-Token": "test-csrf-token"},
        )
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.get_json()["status"], "success")

        public_questions = self.client.get("/api/questions")
        self.assertEqual(public_questions.status_code, 200)
        self.assertTrue(public_questions.get_json())
        self.assertNotIn("correct", public_questions.get_json()[0])

    def test_admin_reports_and_evidence_review_are_isolated_and_auditable(self):
        database.save_report("Test Candidate", 1, 2, 0)
        self.assertEqual(
            len(admin_reviews.get_admin_reports({"candidate": "test candidate"})),
            1,
        )
        self.assertEqual(admin_reviews.get_admin_reports({"candidate": "Other"}), [])

        self.assertTrue(
            admin_reviews.record_proctoring_event(
                "test-exam",
                "Multiple Faces Detected",
                "temporary/test-image.jpg",
                occurred_at=1,
            )
        )
        events = admin_reviews.get_admin_review_events({"status": "unreviewed"})
        self.assertEqual(len(events), 1)
        event_id = events[0]["event_id"]

        self.sign_in_admin()
        update = self.client.post(
            f"/api/admin/evidence-review/{event_id}",
            json={
                "status": "false_positive",
                "note": "Regression test",
            },
            headers={"X-CSRF-Token": "test-csrf-token"},
        )
        self.assertEqual(update.status_code, 200)
        reviewed = admin_reviews.get_admin_review_events(
            {"status": "false_positive"}
        )
        self.assertEqual(reviewed[0]["reviewed_by"], "test-admin")
        self.assertEqual(reviewed[0]["review_note"], "Regression test")

    def test_violation_updates_require_an_active_exam(self):
        unauthorized = self.client.post(
            "/update_violation",
            json={"violations": 1},
        )
        self.assertEqual(unauthorized.status_code, 401)

        questions, resume_token = self.create_exam()
        exam_id = "test-exam"
        self.sign_in_candidate()
        with self.client.session_transaction() as candidate_session:
            candidate_session["active_exam_id"] = exam_id

        with mock.patch.object(exam_app, "_write_monitoring_data", return_value=True):
            updated = self.client.post(
                "/update_violation",
                json={"violations": 3},
            )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(len(database.get_exam_session(exam_id)["questions"]), len(questions))
        self.assertTrue(resume_token)

    def test_cloud_roster_codes_and_candidate_payload_do_not_leak_answers(self):
        roster = b"candidate_name,registration_number\n=Candidate One,REG-100\n"
        prepared = prepare_roster_upload("candidates.csv", roster)
        self.assertEqual(prepared.count, 1)
        record = prepared.records[0]
        credential_rows = list(
            csv.reader(io.StringIO(prepared.credentials_csv.decode("utf-8-sig")))
        )
        access_code = credential_rows[1][2]
        self.assertTrue(
            verify_access_code(
                access_code,
                record["access_code_salt"],
                record["access_code_hash"],
            )
        )
        self.assertIn("'=Candidate One", credential_rows[1][0])
        self.assertNotIn(access_code, str(record))

        snapshot = prepare_question_snapshot(
            [{
                "id": "q1",
                "question": "Question?",
                "options": ["A", "B", "C", "D"],
                "correct_index": 2,
            }]
        )
        candidate_payload = candidate_attempt_payload(
            {
                "id": "attempt-1",
                "attempt_number": 1,
                "status": "in_progress",
                "question_snapshot": snapshot,
                "answers": [None],
                "started_at": "2026-01-01T00:00:00+00:00",
                "deadline_at": "2026-01-01T01:00:00+00:00",
                "last_saved_at": "2026-01-01T00:00:00+00:00",
            }
        )
        self.assertNotIn("correct_index", candidate_payload["questions"][0])
        self.assertNotIn("score", candidate_payload)

    def test_cloud_exam_routes_keep_admin_and_candidate_sessions_separate(self):
        exam_id = "22f3b956-acfb-44c1-b18d-059eaf9246a8"
        admin = self.client.get("/admin/cloud-exams")
        self.assertEqual(admin.status_code, 302)
        self.assertIn("/admin/login", admin.headers["Location"])

        candidate = self.client.post(
            f"/api/cloud-exams/{exam_id}/autosave",
            json={},
        )
        self.assertEqual(candidate.status_code, 401)

    def test_candidate_landing_shows_actionable_error_when_status_storage_fails(self):
        with (
            self.assertLogs(exam_app.app.logger, level="ERROR"),
            mock.patch.object(
                exam_app,
                "get_availability",
                side_effect=NormalExamAvailabilityError("test storage failure"),
            ),
        ):
            response = self.client.get("/")
        self.assertEqual(response.status_code, 503)
        self.assertIn(b"Status unavailable", response.data)


if __name__ == "__main__":
    unittest.main()
