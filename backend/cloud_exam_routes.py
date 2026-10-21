"""Separate Flask routes for Supabase-backed cloud exams."""

from __future__ import annotations

import base64
import binascii
import csv
import hmac
import io
import logging
import secrets
import time
import uuid
from functools import wraps
from typing import Any, Callable

import cv2
import numpy as np
from flask import (
    Blueprint,
    Response,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.exceptions import HTTPException

from backend.cloud_exam_service import (
    CloudExamServiceError,
    SupabaseCloudExamStore,
    candidate_attempt_payload,
)


_MAX_FRAME_BYTES = 250 * 1024
_MAX_FRAME_PIXELS = 1280 * 720
_CAMERA_FRAME_INTERVAL = 2.0
_PROCTOR_EVENT_COOLDOWN = 30.0
_PROCTOR_ANOMALY_SECONDS = 5.0
_FACE_CASCADE_PATH = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
_logger = logging.getLogger(__name__)


def _exam_uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        abort(404)


def _cloud_store() -> SupabaseCloudExamStore:
    cached = current_app.extensions.get("cloud_exam_store")
    if cached is not None:
        return cached
    store = SupabaseCloudExamStore.from_env()
    current_app.extensions["cloud_exam_store"] = store
    return store


def _csrf_matches(expected_key: str, submitted: str | None) -> bool:
    expected = session.get(expected_key)
    return (
        isinstance(expected, str)
        and isinstance(submitted, str)
        and hmac.compare_digest(expected, submitted)
    )


def _admin_csrf_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
        if not _csrf_matches("admin_csrf_token", supplied):
            return "Invalid or missing security token. Reload the admin page and try again.", 400
        return view(*args, **kwargs)

    return wrapped


def _cloud_admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def _candidate_csrf_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
        if not supplied and request.is_json:
            payload = request.get_json(silent=True)
            supplied = payload.get("csrf_token") if isinstance(payload, dict) else None
        if not _csrf_matches("cloud_candidate_csrf_token", supplied):
            return jsonify({"error": "Invalid or missing security token. Reload this page."}), 400
        return view(*args, **kwargs)

    return wrapped


def _candidate_required(view):
    @wraps(view)
    def wrapped(exam_id, *args, **kwargs):
        candidate = session.get("cloud_candidate")
        if not isinstance(candidate, dict) or candidate.get("exam_id") != exam_id:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Sign in with the exam invitation first."}), 401
            return redirect(url_for("cloud_exams.candidate_login", exam_id=exam_id))
        return view(exam_id, *args, **kwargs)

    return wrapped


def _candidate_id() -> str:
    candidate = session.get("cloud_candidate")
    if not isinstance(candidate, dict) or not isinstance(candidate.get("id"), str):
        abort(401)
    return candidate["id"]


def _candidate_attempt(store: SupabaseCloudExamStore, exam_id: str) -> dict[str, Any] | None:
    candidate = session.get("cloud_candidate")
    if not isinstance(candidate, dict):
        return None
    attempt_id = candidate.get("attempt_id")
    if not isinstance(attempt_id, str):
        return None
    return store.get_candidate_attempt(exam_id, _candidate_id(), attempt_id)


def _attempts_remaining(
    exam: dict[str, Any],
    attempt: dict[str, Any] | None,
) -> int:
    attempt_limit = exam.get("attempt_limit")
    attempt_number = attempt.get("attempt_number", 0) if attempt else 0
    if (
        isinstance(attempt_limit, bool)
        or not isinstance(attempt_limit, int)
        or not 1 <= attempt_limit <= 20
        or isinstance(attempt_number, bool)
        or not isinstance(attempt_number, int)
        or not 0 <= attempt_number <= attempt_limit
    ):
        raise CloudExamServiceError("Supabase returned invalid candidate attempt-limit data.")
    return attempt_limit - attempt_number


def _decode_camera_frame(data_url: Any) -> tuple[bytes, np.ndarray]:
    if not isinstance(data_url, str) or not data_url.startswith("data:image/jpeg;base64,"):
        raise CloudExamServiceError("Send a JPEG image from the browser camera.")
    encoded = data_url.partition(",")[2]
    if len(encoded) > ((_MAX_FRAME_BYTES + 2) // 3) * 4:
        raise CloudExamServiceError("The camera frame is too large. Lower camera resolution and retry.")
    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise CloudExamServiceError("The camera frame could not be decoded.") from error
    if not image_bytes or len(image_bytes) > _MAX_FRAME_BYTES:
        raise CloudExamServiceError("The camera frame is too large or empty.")

    frame = cv2.imdecode(np.frombuffer(image_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if frame is None:
        raise CloudExamServiceError("The camera frame is not a valid image.")
    height, width = frame.shape[:2]
    if width < 100 or height < 100 or width * height > _MAX_FRAME_PIXELS:
        raise CloudExamServiceError("Use a camera frame between 100px and 1280×720.")
    return image_bytes, frame


def _camera_request_data() -> dict[str, Any]:
    if request.content_length and request.content_length > 400 * 1024:
        raise CloudExamServiceError("The camera frame request is too large.")
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise CloudExamServiceError("A camera frame is required.")
    return data


def _detect_faces(frame: np.ndarray) -> tuple[int, np.ndarray]:
    detector = cv2.CascadeClassifier(_FACE_CASCADE_PATH)
    if detector.empty():
        raise RuntimeError("The OpenCV face-detection model could not be loaded.")
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    faces = detector.detectMultiScale(
        gray,
        scaleFactor=1.12,
        minNeighbors=5,
        minSize=(40, 40),
    )
    return len(faces), faces


def _render_candidate_error(exam_id: str, message: str, status: int = 200):
    return render_template(
        "cloud_candidate_login.html",
        exam_id=exam_id,
        exam=None,
        csrf_token=session.setdefault(
            "cloud_candidate_csrf_token", secrets.token_urlsafe(32)
        ),
        error=message,
    ), status


def _int_field(name: str, label: str, minimum: int, maximum: int) -> int:
    raw = request.form.get(name, "")
    if not raw.isdigit():
        raise CloudExamServiceError(f"{label} must be a whole number.")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise CloudExamServiceError(
            f"{label} must be between {minimum} and {maximum}."
        )
    return value


def create_cloud_exam_blueprint(
    question_provider: Callable[[], list[dict[str, Any]]],
) -> Blueprint:
    blueprint = Blueprint("cloud_exams", __name__)

    @blueprint.get("/admin/cloud-exams")
    @_cloud_admin_required
    def admin_index():
        exams: list[dict[str, Any]] = []
        questions: list[dict[str, Any]] = []
        setup_error = None
        try:
            store = _cloud_store()
            exams = store.list_admin_exams()
        except CloudExamServiceError as error:
            setup_error = str(error)
        except Exception:
            _logger.exception("Could not load the cloud exam admin list.")
            setup_error = "Cloud exam storage is unavailable. Check the server logs and retry."
        try:
            questions = question_provider()
        except Exception:
            _logger.exception("Could not load the administrator question bank.")
            if setup_error is None:
                setup_error = "The question bank could not be loaded. Try again before creating an exam."

        return render_template(
            "admin_cloud_exams.html",
            exams=exams,
            questions=questions,
            setup_error=setup_error,
            csrf_token=session.get("admin_csrf_token", ""),
            admin_username=session.get("admin_username", "Admin"),
        )

    @blueprint.post("/admin/cloud-exams")
    @_cloud_admin_required
    @_admin_csrf_required
    def create_exam():
        try:
            all_questions = question_provider()
            selected_ids = set(request.form.getlist("question_ids"))
            selected_questions = [
                question
                for question in all_questions
                if str(question.get("id")) in selected_ids
            ]
            if len(selected_questions) != len(selected_ids):
                raise CloudExamServiceError("One or more selected questions are no longer available.")

            title = request.form.get("title", "").strip()
            opens_at = request.form.get("opens_at_utc", "")
            closes_at = request.form.get("closes_at_utc", "")
            store = _cloud_store()
            exam = store.create_exam(
                title=title,
                opens_at=opens_at,
                closes_at=closes_at,
                duration_minutes=_int_field("duration_minutes", "Duration", 1, 480),
                attempt_limit=_int_field("attempt_limit", "Attempt limit", 1, 20),
                pass_marks=_int_field("pass_marks", "Pass marks", 0, 100),
                randomize_questions=request.form.get("randomize_questions") == "on",
                questions=selected_questions,
                created_by=str(session.get("admin_username", "Admin")),
            )
        except CloudExamServiceError as error:
            flash(str(error), "error")
            return redirect(url_for("cloud_exams.admin_index"))
        except Exception:
            _logger.exception("Could not create the cloud exam.")
            flash("The cloud exam could not be created. Check the server logs and retry.", "error")
            return redirect(url_for("cloud_exams.admin_index"))

        flash("Cloud exam created as a draft. Import its roster before opening it.", "success")
        return redirect(url_for("cloud_exams.admin_detail", exam_id=exam["id"]))

    @blueprint.get("/admin/cloud-exams/<exam_id>")
    @_cloud_admin_required
    def admin_detail(exam_id: str):
        exam_id = _exam_uuid(exam_id)
        try:
            detail = _cloud_store().get_admin_exam_detail(exam_id)
        except CloudExamServiceError as error:
            return render_template(
                "admin_cloud_exam_detail.html",
                detail=None,
                error=str(error),
                csrf_token=session.get("admin_csrf_token", ""),
                admin_username=session.get("admin_username", "Admin"),
            ), 503
        except Exception:
            _logger.exception("Could not load cloud exam details.")
            return render_template(
                "admin_cloud_exam_detail.html",
                detail=None,
                error="Cloud exam details are unavailable. Check the server logs and retry.",
                csrf_token=session.get("admin_csrf_token", ""),
                admin_username=session.get("admin_username", "Admin"),
            ), 503

        if detail is None:
            abort(404)
        candidates = detail.get("candidates")
        attempts = detail.get("attempts")
        if not isinstance(candidates, list) or not isinstance(attempts, list):
            _logger.error("Supabase returned invalid cloud exam detail data.")
            abort(503)
        exam = detail.get("exam")
        if not isinstance(exam, dict):
            _logger.error("Supabase returned invalid cloud exam metadata.")
            abort(503)
        detail["candidate_total"] = int(detail.get("candidate_total", len(candidates)))
        detail["attempt_total"] = int(detail.get("attempt_total", len(attempts)))
        detail["candidates_truncated"] = detail["candidate_total"] > len(candidates)
        detail["attempts_truncated"] = detail["attempt_total"] > len(attempts)
        detail["candidates"] = candidates[:100]
        detail["attempts"] = attempts[:100]
        return render_template(
            "admin_cloud_exam_detail.html",
            detail=detail,
            error=None,
            csrf_token=session.get("admin_csrf_token", ""),
            admin_username=session.get("admin_username", "Admin"),
        )

    @blueprint.post("/admin/cloud-exams/<exam_id>/roster")
    @_cloud_admin_required
    @_admin_csrf_required
    def import_roster(exam_id: str):
        exam_id = _exam_uuid(exam_id)
        if request.content_length and request.content_length > 5 * 1024 * 1024 + 64 * 1024:
            flash("Roster file must be 5 MB or smaller.", "error")
            return redirect(url_for("cloud_exams.admin_detail", exam_id=exam_id))
        uploaded = request.files.get("roster")
        if uploaded is None or not uploaded.filename:
            flash("Choose a CSV or XLSX roster file.", "error")
            return redirect(url_for("cloud_exams.admin_detail", exam_id=exam_id))

        try:
            content = uploaded.stream.read(5 * 1024 * 1024 + 1)
            prepared = _cloud_store().import_roster(
                exam_id=exam_id,
                filename=uploaded.filename,
                content=content,
                actor=str(session.get("admin_username", "Admin")),
            )
        except CloudExamServiceError as error:
            flash(str(error), "error")
            return redirect(url_for("cloud_exams.admin_detail", exam_id=exam_id))
        except Exception:
            _logger.exception("Cloud exam roster import failed.")
            flash(
                "The roster was not imported. Check the server logs; invitation codes were not downloaded.",
                "error",
            )
            return redirect(url_for("cloud_exams.admin_detail", exam_id=exam_id))

        response = Response(prepared.credentials_csv, mimetype="text/csv")
        response.headers["Content-Disposition"] = (
            f'attachment; filename="cloud-exam-{exam_id}-candidate-access-once.csv"'
        )
        response.headers["Cache-Control"] = "no-store, private"
        response.headers["Pragma"] = "no-cache"
        response.headers["X-Roster-Count"] = str(prepared.count)
        return response

    @blueprint.post("/admin/cloud-exams/<exam_id>/status")
    @_cloud_admin_required
    @_admin_csrf_required
    def change_exam_status(exam_id: str):
        exam_id = _exam_uuid(exam_id)
        target_status = request.form.get("status", "")
        if target_status not in {"open", "closed"}:
            flash("Choose Open or Close for the cloud exam.", "error")
            return redirect(url_for("cloud_exams.admin_detail", exam_id=exam_id))
        try:
            _cloud_store().set_exam_status(
                exam_id,
                target_status,
                str(session.get("admin_username", "Admin")),
            )
        except CloudExamServiceError as error:
            flash(str(error), "error")
        except Exception:
            _logger.exception("Could not update cloud exam status.")
            flash("The exam status could not be changed. Check the server logs and retry.", "error")
        else:
            flash(f"Cloud exam is now {target_status}.", "success")
        return redirect(url_for("cloud_exams.admin_detail", exam_id=exam_id))

    @blueprint.post("/admin/cloud-exams/<exam_id>/publish")
    @_cloud_admin_required
    @_admin_csrf_required
    def publish_exam_results(exam_id: str):
        exam_id = _exam_uuid(exam_id)
        try:
            _cloud_store().publish_results(
                exam_id,
                str(session.get("admin_username", "Admin")),
            )
        except CloudExamServiceError as error:
            flash(str(error), "error")
        except Exception:
            _logger.exception("Cloud exam results could not be published.")
            flash("Results could not be published. Check the server logs and retry.", "error")
        else:
            flash("Results are now available to candidates on this exam.", "success")
        return redirect(url_for("cloud_exams.admin_detail", exam_id=exam_id))

    @blueprint.get("/admin/cloud-exams/<exam_id>/results.csv")
    @_cloud_admin_required
    def export_exam_results(exam_id: str):
        exam_id = _exam_uuid(exam_id)
        try:
            rows = _cloud_store().export_exam_results(exam_id)
        except Exception:
            _logger.exception("Could not export cloud exam results.")
            return "Results export is unavailable. Check the server logs and retry.", 503

        buffer = io.StringIO(newline="")
        writer = csv.writer(buffer)
        writer.writerow(
            (
                "Candidate name",
                "Registration number",
                "Attempt",
                "Status",
                "Score",
                "Total questions",
                "Passed",
                "Started at",
                "Submitted at",
            )
        )
        for row in rows:
            candidate_name = str(row.get("candidate_name") or "")
            registration_number = str(row.get("registration_number") or "")
            if candidate_name.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
                candidate_name = "'" + candidate_name
            if registration_number.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
                registration_number = "'" + registration_number
            writer.writerow(
                (
                    candidate_name,
                    registration_number,
                    row.get("attempt_number"),
                    row.get("status"),
                    row.get("score"),
                    row.get("total_questions"),
                    row.get("passed"),
                    row.get("started_at"),
                    row.get("submitted_at"),
                )
            )

        response = Response(
            "\ufeff" + buffer.getvalue(),
            mimetype="text/csv",
        )
        response.headers["Content-Disposition"] = (
            f'attachment; filename="cloud-exam-{exam_id}-results.csv"'
        )
        response.headers["Cache-Control"] = "no-store, private"
        return response

    @blueprint.get("/cloud-exams/<exam_id>")
    def candidate_login(exam_id: str):
        exam_id = _exam_uuid(exam_id)
        csrf_token = session.setdefault(
            "cloud_candidate_csrf_token", secrets.token_urlsafe(32)
        )
        try:
            exam = _cloud_store().get_public_exam(exam_id)
        except CloudExamServiceError as error:
            return _render_candidate_error(exam_id, str(error), 503)
        except Exception:
            _logger.exception("Could not load public cloud exam information.")
            return _render_candidate_error(
                exam_id,
                "Cloud exam details are unavailable. Check the server logs and retry.",
                503,
            )
        if exam is None:
            abort(404)

        candidate = session.get("cloud_candidate")
        if isinstance(candidate, dict) and candidate.get("exam_id") == exam_id:
            if candidate.get("attempt_id"):
                try:
                    attempt = _candidate_attempt(_cloud_store(), exam_id)
                except Exception:
                    _logger.exception("Could not restore the candidate cloud exam attempt.")
                    return _render_candidate_error(
                        exam_id,
                        "Your attempt could not be restored. Retry in a moment.",
                        503,
                    )
                if attempt and attempt.get("status") == "submitted":
                    try:
                        remaining = _attempts_remaining(exam, attempt)
                    except CloudExamServiceError:
                        _logger.exception("Cloud candidate attempt-limit data is invalid.")
                        return _render_candidate_error(
                            exam_id,
                            "The exam attempt limit could not be checked. Contact the administrator.",
                            503,
                        )
                    destination = (
                        "cloud_exams.candidate_instructions"
                        if remaining
                        else "cloud_exams.candidate_result"
                    )
                    return redirect(url_for(destination, exam_id=exam_id))
                if attempt and attempt.get("status") == "in_progress":
                    return redirect(url_for("cloud_exams.candidate_exam", exam_id=exam_id))
            return redirect(url_for("cloud_exams.candidate_instructions", exam_id=exam_id))

        return render_template(
            "cloud_candidate_login.html",
            exam=exam,
            exam_id=exam_id,
            csrf_token=csrf_token,
            error=None,
        )

    @blueprint.post("/cloud-exams/<exam_id>/access")
    @_candidate_csrf_required
    def candidate_access(exam_id: str):
        exam_id = _exam_uuid(exam_id)
        registration_number = request.form.get("registration_number", "")
        access_code = request.form.get("access_code", "")
        if (
            not registration_number.strip()
            or len(registration_number) > 128
            or not access_code.strip()
            or len(access_code) > 100
        ):
            flash("Enter the registration number and invitation code from your administrator.", "error")
            return redirect(url_for("cloud_exams.candidate_login", exam_id=exam_id))
        try:
            store = _cloud_store()
            secret_key = current_app.secret_key
            secret_key_text = (
                secret_key.decode("utf-8")
                if isinstance(secret_key, bytes)
                else str(secret_key)
            )
            permitted = store.allow_candidate_login(
                exam_id=exam_id,
                registration_number=registration_number,
                source_address=request.remote_addr or "unknown",
                secret_key=secret_key_text,
            )
            candidate = (
                store.authenticate_candidate(
                    exam_id=exam_id,
                    registration_number=registration_number,
                    access_code=access_code,
                )
                if permitted
                else None
            )
        except Exception:
            _logger.exception("Cloud candidate access verification is unavailable.")
            return _render_candidate_error(
                exam_id,
                "Access verification is temporarily unavailable. Retry in a moment.",
                503,
            )

        if candidate is None:
            flash(
                "The registration number or invitation code is incorrect, or too many attempts were made. Contact your administrator if needed.",
                "error",
            )
            return redirect(url_for("cloud_exams.candidate_login", exam_id=exam_id))

        session.pop("cloud_candidate", None)
        session["cloud_candidate"] = {
            "id": candidate["id"],
            "exam_id": exam_id,
            "candidate_name": candidate["candidate_name"],
        }
        session["cloud_candidate_csrf_token"] = secrets.token_urlsafe(32)
        session.permanent = True
        return redirect(url_for("cloud_exams.candidate_instructions", exam_id=exam_id))

    @blueprint.get("/cloud-exams/<exam_id>/instructions")
    @_candidate_required
    def candidate_instructions(exam_id: str):
        exam_id = _exam_uuid(exam_id)
        candidate = session["cloud_candidate"]
        try:
            store = _cloud_store()
            exam = store.get_public_exam(exam_id)
            if exam is None:
                abort(404)
            attempt = _candidate_attempt(store, exam_id)
            attempts_remaining = _attempts_remaining(exam, attempt)
        except HTTPException:
            raise
        except CloudExamServiceError as error:
            return _render_candidate_error(exam_id, str(error), 503)
        except Exception:
            _logger.exception("Could not check cloud candidate attempt availability.")
            return "Exam attempt availability is temporarily unavailable. Reload to retry.", 503
        if attempt and attempt.get("status") == "submitted" and not attempts_remaining:
            return redirect(url_for("cloud_exams.candidate_result", exam_id=exam_id))
        if attempt and attempt.get("status") == "in_progress":
            return redirect(url_for("cloud_exams.candidate_exam", exam_id=exam_id))
        session.pop("cloud_exam_ready", None)
        return render_template(
            "cloud_candidate_instructions.html",
            exam_id=exam_id,
            candidate_name=candidate.get("candidate_name", "Candidate"),
            csrf_token=session.get("cloud_candidate_csrf_token", ""),
            attempts_remaining=attempts_remaining,
            next_attempt_number=(attempt.get("attempt_number", 0) + 1) if attempt else 1,
            attempt_limit=exam["attempt_limit"],
            error=None,
        )

    @blueprint.post("/api/cloud-exams/<exam_id>/readiness")
    @_candidate_required
    @_candidate_csrf_required
    def candidate_readiness(exam_id: str):
        exam_id = _exam_uuid(exam_id)
        try:
            data = _camera_request_data()
            _, frame = _decode_camera_frame(data.get("frame"))
            face_count, _ = _detect_faces(frame)
        except CloudExamServiceError as error:
            return jsonify({"error": str(error)}), 400
        except Exception:
            _logger.exception("Cloud exam camera readiness check failed.")
            return jsonify({"error": "Camera face detection is unavailable. Retry or contact the administrator."}), 503

        ready = face_count == 1
        if ready:
            session["cloud_exam_ready"] = {
                "exam_id": exam_id,
                "candidate_id": _candidate_id(),
                "checked_at": time.time(),
            }
        else:
            session.pop("cloud_exam_ready", None)
        return jsonify({
            "ready": ready,
            "face_count": face_count,
            "message": "One face detected." if ready else (
                "No face detected. Face the camera with good lighting."
                if face_count == 0
                else "More than one face detected. Only the candidate should be visible."
            ),
        })

    @blueprint.post("/cloud-exams/<exam_id>/start")
    @_candidate_required
    @_candidate_csrf_required
    def candidate_start(exam_id: str):
        exam_id = _exam_uuid(exam_id)
        ready_state = session.get("cloud_exam_ready")
        if (
            not isinstance(ready_state, dict)
            or ready_state.get("exam_id") != exam_id
            or ready_state.get("candidate_id") != _candidate_id()
            or time.time() - float(ready_state.get("checked_at", 0)) > 60
        ):
            return jsonify({"error": "Complete the live camera readiness check before starting."}), 409
        try:
            attempt = _cloud_store().start_attempt(exam_id, _candidate_id())
            safe_attempt = candidate_attempt_payload(attempt)
        except CloudExamServiceError as error:
            return jsonify({"error": str(error)}), 409
        except Exception:
            _logger.exception("Could not start the cloud candidate attempt.")
            return jsonify({"error": "The exam could not be started. Check your connection and retry."}), 503

        session["cloud_candidate"]["attempt_id"] = safe_attempt["attempt_id"]
        session.pop("cloud_exam_ready", None)
        return jsonify({"status": "started", "redirect": url_for("cloud_exams.candidate_exam", exam_id=exam_id)})

    @blueprint.get("/cloud-exams/<exam_id>/exam")
    @_candidate_required
    def candidate_exam(exam_id: str):
        exam_id = _exam_uuid(exam_id)
        try:
            attempt = _candidate_attempt(_cloud_store(), exam_id)
        except Exception:
            _logger.exception("Could not load the cloud exam attempt.")
            return "The exam session is temporarily unavailable. Reload to retry.", 503
        if not attempt:
            return redirect(url_for("cloud_exams.candidate_instructions", exam_id=exam_id))
        if attempt.get("status") == "submitted":
            return redirect(url_for("cloud_exams.candidate_result", exam_id=exam_id))
        try:
            safe_attempt = candidate_attempt_payload(attempt)
        except CloudExamServiceError:
            _logger.exception("Cloud attempt data is invalid.")
            return "The saved exam session is invalid. Contact the administrator.", 503
        return render_template(
            "cloud_candidate_exam.html",
            exam_id=exam_id,
            candidate_name=session["cloud_candidate"].get("candidate_name", "Candidate"),
            attempt=safe_attempt,
            csrf_token=session.get("cloud_candidate_csrf_token", ""),
        )

    @blueprint.post("/api/cloud-exams/<exam_id>/autosave")
    @_candidate_required
    @_candidate_csrf_required
    def candidate_autosave(exam_id: str):
        exam_id = _exam_uuid(exam_id)
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Exam progress must be a JSON object."}), 400
        attempt_id = session["cloud_candidate"].get("attempt_id")
        if not isinstance(attempt_id, str):
            return jsonify({"error": "There is no active cloud exam attempt."}), 409
        try:
            result = _cloud_store().save_answers(
                exam_id,
                _candidate_id(),
                attempt_id,
                data.get("answers"),
            )
        except CloudExamServiceError as error:
            return jsonify({"error": str(error)}), 400
        except Exception:
            _logger.exception("Cloud exam autosave failed.")
            return jsonify({"error": "Answers could not be saved. Check your connection and retry."}), 503
        return jsonify(result)

    @blueprint.post("/api/cloud-exams/<exam_id>/submit")
    @_candidate_required
    @_candidate_csrf_required
    def candidate_submit(exam_id: str):
        exam_id = _exam_uuid(exam_id)
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "Exam submission must be a JSON object."}), 400
        attempt_id = session["cloud_candidate"].get("attempt_id")
        if not isinstance(attempt_id, str):
            return jsonify({"error": "There is no active cloud exam attempt."}), 409
        reason = data.get("reason", "manual")
        try:
            result = _cloud_store().submit_attempt(
                exam_id,
                _candidate_id(),
                attempt_id,
                data.get("answers"),
                reason,
            )
        except CloudExamServiceError as error:
            return jsonify({"error": str(error)}), 400
        except Exception:
            _logger.exception("Cloud exam submission failed.")
            return jsonify({"error": "The exam could not be submitted. Check your connection and retry."}), 503
        session["cloud_candidate"]["attempt_id"] = attempt_id
        return jsonify({
            "status": result.get("status"),
            "redirect": url_for("cloud_exams.candidate_result", exam_id=exam_id),
        })

    @blueprint.post("/api/cloud-exams/<exam_id>/proctor/frame")
    @_candidate_required
    @_candidate_csrf_required
    def candidate_proctor_frame(exam_id: str):
        exam_id = _exam_uuid(exam_id)
        now = time.time()
        last_frame_at = float(session.get("cloud_last_frame_at", 0))
        if now - last_frame_at < _CAMERA_FRAME_INTERVAL:
            return jsonify({"error": "Camera frames are arriving too quickly."}), 429
        session["cloud_last_frame_at"] = now

        attempt_id = session["cloud_candidate"].get("attempt_id")
        if not isinstance(attempt_id, str):
            return jsonify({"error": "There is no active cloud exam attempt."}), 409
        try:
            data = _camera_request_data()
            image_bytes, frame = _decode_camera_frame(data.get("frame"))
            face_count, _ = _detect_faces(frame)
        except CloudExamServiceError as error:
            return jsonify({"error": str(error)}), 400
        except Exception:
            _logger.exception("Cloud exam camera monitoring failed.")
            return jsonify({"error": "Camera monitoring is unavailable. Retry or contact the administrator."}), 503

        anomaly_key = "no_face" if face_count == 0 else (
            "multiple_faces" if face_count > 1 else None
        )
        previous_anomaly = session.get("cloud_face_anomaly")
        if anomaly_key:
            if not isinstance(previous_anomaly, dict) or previous_anomaly.get("type") != anomaly_key:
                session["cloud_face_anomaly"] = {"type": anomaly_key, "started_at": now}
                return jsonify({"face_count": face_count, "event_recorded": False})
            anomaly_started = float(previous_anomaly.get("started_at", now))
            previous_event_at = float(session.get("cloud_last_proctor_event_at", 0))
            if (
                now - anomaly_started >= _PROCTOR_ANOMALY_SECONDS
                and now - previous_event_at >= _PROCTOR_EVENT_COOLDOWN
            ):
                try:
                    _cloud_store().record_proctoring_event(
                        exam_id,
                        attempt_id,
                        anomaly_key,
                        image_bytes,
                    )
                except Exception:
                    _logger.exception("Could not store cloud exam proctoring evidence.")
                    return jsonify({
                        "face_count": face_count,
                        "event_recorded": False,
                        "error": "The monitoring event could not be stored. Check your connection.",
                    }), 503
                session["cloud_last_proctor_event_at"] = now
                return jsonify({"face_count": face_count, "event_recorded": True})
        else:
            session.pop("cloud_face_anomaly", None)
        return jsonify({"face_count": face_count, "event_recorded": False})

    @blueprint.get("/cloud-exams/<exam_id>/result")
    @_candidate_required
    def candidate_result(exam_id: str):
        exam_id = _exam_uuid(exam_id)
        candidate = session["cloud_candidate"]
        attempt_id = candidate.get("attempt_id")
        if not isinstance(attempt_id, str):
            return redirect(url_for("cloud_exams.candidate_instructions", exam_id=exam_id))
        attempts_remaining = 0
        try:
            store = _cloud_store()
            exam = store.get_public_exam(exam_id)
            if exam is None:
                abort(404)
            attempt = _candidate_attempt(store, exam_id)
            if not attempt:
                return redirect(url_for("cloud_exams.candidate_instructions", exam_id=exam_id))
            if attempt.get("status") == "in_progress":
                return redirect(url_for("cloud_exams.candidate_exam", exam_id=exam_id))
            attempts_remaining = _attempts_remaining(exam, attempt)
            result = store.get_published_candidate_result(exam_id, _candidate_id())
        except HTTPException:
            raise
        except Exception:
            _logger.exception("Could not load published candidate cloud results.")
            result = None
            error = "Your result could not be checked. Reload to try again."
        else:
            error = None
        return render_template(
            "cloud_candidate_result.html",
            result=result,
            error=error,
            exam_id=exam_id,
            candidate_name=candidate.get("candidate_name", "Candidate"),
            attempts_remaining=attempts_remaining,
        )

    return blueprint
