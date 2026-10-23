from flask import (
    Flask, render_template, Response, send_file, redirect,
    send_from_directory, request, session, jsonify, url_for
)
import subprocess
import os
import json
import sys
import sqlite3
import base64
import binascii
import time
import hmac
import hashlib
import secrets
import tempfile
import threading
import csv
import io
from datetime import date, datetime, timedelta
from html import escape as html_escape
from functools import wraps
import cv2
import numpy as np
from PIL import Image, UnidentifiedImageError
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib import colors
from reportlab.platypus import LongTable, TableStyle
from reportlab.lib.pagesizes import letter

if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="backslashreplace")

sys.path.append(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

from backend.database import (
    init_db,
    get_all_reports,
    get_all_questions,
    add_question,
    delete_question,
    create_exam_session,
    get_exam_session,
    get_exam_session_by_token_hash,
    rotate_exam_resume_token,
    save_exam_progress,
    complete_exam_session,
    supabase,
    DB_NAME,
)
from backend.admin_reviews import (
    REVIEW_DECISIONS,
    get_admin_reports,
    get_admin_review_events,
    init_admin_review_db,
    record_proctoring_event,
    register_unassigned_evidence,
    update_evidence_review,
)
from backend.cloud_exam_routes import create_cloud_exam_blueprint

_monitor_stats_lock = threading.Lock()

# Base directory of frontend
BASE_DIR = os.path.dirname(
    os.path.dirname(
        os.path.abspath(__file__)
    )
)

app = Flask(
    __name__,
    template_folder=os.path.join(BASE_DIR, "frontend", "templates"),
    static_folder=os.path.join(BASE_DIR, "frontend", "static")
)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)
if not os.getenv("FLASK_SECRET_KEY"):
    app.logger.warning(
        "FLASK_SECRET_KEY is not configured; sessions will expire when the app restarts."
    )
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "").lower() == "true",
    PERMANENT_SESSION_LIFETIME=timedelta(hours=24),
)
MAX_LOGIN_FRAME_BYTES = 2 * 1024 * 1024
MAX_LOGIN_FRAME_BASE64_LENGTH = ((MAX_LOGIN_FRAME_BYTES + 2) // 3) * 4
MAX_LOGIN_FRAME_WIDTH = 1920
MAX_LOGIN_FRAME_HEIGHT = 1080
init_db()
init_admin_review_db()
app.register_blueprint(create_cloud_exam_blueprint(get_all_questions))


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Admin login required"}), 401
            return redirect(url_for("admin_login"))
        return view(*args, **kwargs)

    return wrapped


def admin_csrf_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        expected_token = session.get("admin_csrf_token", "")
        submitted_token = (
            request.form.get("csrf_token")
            or request.headers.get("X-CSRF-Token", "")
        )
        if not expected_token or not submitted_token or not hmac.compare_digest(
            expected_token,
            submitted_token
        ):
            if request.is_json:
                return jsonify({"error": "Invalid or missing CSRF token"}), 400
            return (
                "Your form expired or could not be verified. Reload the admin page and try again.",
                400,
            )
        return view(*args, **kwargs)

    return wrapped


def append_violation_log(violation_type, evidence_path):
    log_path = os.path.join(
        BASE_DIR,
        "backend",
        "evidence",
        "violation_log.txt"
    )
    relative_evidence_path = os.path.relpath(
        evidence_path,
        BASE_DIR
    ).replace(os.sep, "/")

    with open(log_path, "a", encoding="utf-8") as log:
        log.write(
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
            f"VIOLATION: {violation_type}\n"
        )
        log.write(f"File: {relative_evidence_path}\n")
        log.write("Severity: High\n")
        log.write("Status: Logged\n\n")


def _store_evidence_review_event(exam_id, event_type, evidence_path, occurred_at):
    evidence_folder = os.path.join(BASE_DIR, "backend", "evidence")
    relative_path = os.path.relpath(
        evidence_path,
        evidence_folder,
    ).replace(os.sep, "/")
    try:
        record_proctoring_event(
            exam_id,
            event_type,
            relative_path,
            occurred_at,
        )
    except sqlite3.Error:
        app.logger.exception(
            "Could not add evidence %s to the admin review queue.",
            relative_path,
        )


def _requested_report_filters():
    filters = {
        "candidate": request.args.get("candidate", "").strip(),
        "exam_id": request.args.get("exam_id", "").strip(),
        "date_from": request.args.get("date_from", "").strip(),
        "date_to": request.args.get("date_to", "").strip(),
    }
    if (
        len(filters["candidate"]) > 120
        or len(filters["exam_id"]) > 128
    ):
        raise ValueError("Candidate or exam filter is too long.")

    for key in ("date_from", "date_to"):
        value = filters[key]
        if value:
            parsed = date.fromisoformat(value)
            if parsed.isoformat() != value:
                raise ValueError("Dates must use YYYY-MM-DD format.")
    if (
        filters["date_from"]
        and filters["date_to"]
        and filters["date_from"] > filters["date_to"]
    ):
        raise ValueError("The start date must be on or before the end date.")
    return filters


def _requested_event_filters():
    filters = {
        "candidate": request.args.get("candidate", "").strip(),
        "exam_id": request.args.get("exam_id", "").strip(),
        "status": request.args.get("status", "all").strip(),
    }
    if len(filters["candidate"]) > 120 or len(filters["exam_id"]) > 128:
        raise ValueError("Candidate or exam filter is too long.")
    if filters["status"] not in REVIEW_DECISIONS | {"all", "unreviewed"}:
        raise ValueError("Select a valid review status.")
    return filters


def _hash_resume_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _candidate_face_reference_path(reg_no):
    registration_key = _candidate_registration_key(reg_no)
    return os.path.join(
        BASE_DIR,
        "dataset",
        "face_enrollments",
        f"{registration_key}.jpg",
    )


def _candidate_registration_key(reg_no):
    return hashlib.sha256(
        reg_no.strip().casefold().encode("utf-8")
    ).hexdigest()


def _decode_candidate_camera_frame(frame_data):
    if not isinstance(frame_data, str) or not frame_data:
        raise ValueError("A camera frame is required.")
    if len(frame_data) > MAX_LOGIN_FRAME_BASE64_LENGTH:
        raise ValueError("The camera frame is too large.")

    try:
        image_data = base64.b64decode(frame_data, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("The camera frame is not valid base64 data.") from error

    if not image_data or len(image_data) > MAX_LOGIN_FRAME_BYTES:
        raise ValueError("The camera frame is empty or too large.")

    try:
        with Image.open(io.BytesIO(image_data)) as image:
            if image.format != "JPEG":
                raise ValueError("The camera frame must be a JPEG image.")
            width, height = image.size
            if (
                width < 1
                or height < 1
                or width > MAX_LOGIN_FRAME_WIDTH
                or height > MAX_LOGIN_FRAME_HEIGHT
            ):
                raise ValueError("The camera frame dimensions are not supported.")
            rgb_frame = np.asarray(image.convert("RGB"))
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        raise ValueError("The camera frame could not be decoded.") from error

    return cv2.cvtColor(rgb_frame, cv2.COLOR_RGB2BGR)


def _get_supabase_candidate_profile(registration_key):
    response = (
        supabase.table("candidate_profiles")
        .select("registration_key,candidate_name,reg_no,face_image_path")
        .eq("registration_key", registration_key)
        .limit(1)
        .execute()
    )
    return response.data[0] if response.data else None


def _download_supabase_face(profile, registration_key, reference_path):
    image_path = profile.get("face_image_path")
    expected_prefix = f"face_enrollments/{registration_key}/"
    if not isinstance(image_path, str) or not image_path.startswith(expected_prefix):
        raise ValueError("The stored candidate face path is invalid.")

    image_data = supabase.storage.from_("candidate-faces").download(image_path)
    if not isinstance(image_data, (bytes, bytearray)) or not image_data:
        raise OSError("Supabase returned an empty candidate face image.")

    directory = os.path.dirname(reference_path)
    os.makedirs(directory, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=directory,
            suffix=".jpg",
            delete=False,
        ) as image_file:
            temporary_path = image_file.name
            image_file.write(image_data)
            image_file.flush()
            os.fsync(image_file.fileno())
        os.replace(temporary_path, reference_path)
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.remove(temporary_path)


def _save_local_face_reference(frame, reference_path, replace_existing=False):
    encoded, image_data = cv2.imencode(".jpg", frame)
    if not encoded:
        raise OSError("Could not encode the enrolled face image.")

    directory = os.path.dirname(reference_path)
    os.makedirs(directory, exist_ok=True)
    if not replace_existing:
        with open(reference_path, "xb") as image_file:
            image_file.write(bytes(image_data))
            image_file.flush()
            os.fsync(image_file.fileno())
        return

    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=directory,
            suffix=".jpg",
            delete=False,
        ) as image_file:
            temporary_path = image_file.name
            image_file.write(bytes(image_data))
            image_file.flush()
            os.fsync(image_file.fileno())

        os.replace(temporary_path, reference_path)
        temporary_path = None
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.remove(temporary_path)


def _remove_supabase_face(image_path):
    try:
        supabase.storage.from_("candidate-faces").remove([image_path])
    except Exception:
        app.logger.warning("Could not remove an unused candidate face upload from Supabase.")


def _save_candidate_profile_to_supabase(name, reg_no, registration_key, reference_path):
    image_path = (
        f"face_enrollments/{registration_key}/{secrets.token_hex(16)}.jpg"
    )
    with open(reference_path, "rb") as image_file:
        supabase.storage.from_("candidate-faces").upload(
            path=image_path,
            file=image_file.read(),
            file_options={
                "content-type": "image/jpeg",
                "upsert": "false",
            },
        )

    profile = {
        "registration_key": registration_key,
        "candidate_name": name,
        "reg_no": reg_no,
        "face_image_path": image_path,
    }
    try:
        supabase.table("candidate_profiles").insert(profile).execute()
        return profile, True
    except Exception as insert_error:
        try:
            existing_profile = _get_supabase_candidate_profile(registration_key)
        except Exception as lookup_error:
            _remove_supabase_face(image_path)
            raise RuntimeError(
                "Supabase could not confirm whether the candidate profile was saved."
            ) from lookup_error
        if existing_profile is None:
            _remove_supabase_face(image_path)
            raise insert_error
        _remove_supabase_face(image_path)
        return existing_profile, False


def _same_candidate_name(first_name, second_name):
    return " ".join(first_name.split()).casefold() == " ".join(
        second_name.split()
    ).casefold()


def _face_matches_reference(frame, reference_path):
    from deepface import DeepFace

    verification = DeepFace.verify(
        img1_path=frame,
        img2_path=reference_path,
        model_name="Facenet512",
        detector_backend="opencv",
        distance_metric="cosine",
        enforce_detection=True,
        silent=True,
    )
    return bool(verification.get("verified"))


def _set_exam_session(exam_record):
    session["active_exam_id"] = exam_record["exam_id"]
    session["candidate_name"] = exam_record["candidate_name"]
    session["reg_no"] = exam_record["reg_no"]
    session.permanent = True


def _public_exam_questions(exam_record):
    return [
        {
            "id": question["id"],
            "question": question["question"],
            "options": question["options"],
        }
        for question in exam_record["questions"]
    ]


def _prepare_exam_questions(questions):
    if not isinstance(questions, list) or not questions:
        return None

    prepared = []
    for question in questions:
        if (
            not isinstance(question, dict)
            or "id" not in question
            or not isinstance(question.get("question"), str)
            or not question["question"].strip()
            or not isinstance(question.get("options"), list)
            or len(question["options"]) != 4
            or any(
                not isinstance(option, str) or not option.strip()
                for option in question["options"]
            )
        ):
            return None
        prepared.append({
            "id": question["id"],
            "question": question["question"],
            "options": question["options"],
        })
    return prepared


def _validated_exam_progress(data, exam_record):
    if not isinstance(data, dict):
        return None

    question_count = len(exam_record["questions"])
    question_ids = data.get("question_ids")
    answers = data.get("answers")
    reviewed = data.get("reviewed")
    visited = data.get("visited")
    current_question = data.get("current_question")

    expected_ids = [str(question["id"]) for question in exam_record["questions"]]
    if (
        not isinstance(question_ids, list)
        or [str(question_id) for question_id in question_ids] != expected_ids
    ):
        return None
    if (
        not isinstance(answers, list)
        or len(answers) != question_count
        or not isinstance(reviewed, list)
        or len(reviewed) != question_count
        or not isinstance(visited, list)
        or len(visited) != question_count
    ):
        return None
    if any(
        answer is not None
        and (
            isinstance(answer, bool)
            or not isinstance(answer, int)
            or answer < 0
            or answer >= len(exam_record["questions"][index]["options"])
        )
        for index, answer in enumerate(answers)
    ):
        return None
    if any(not isinstance(flag, bool) for flag in reviewed + visited):
        return None
    if (
        isinstance(current_question, bool)
        or not isinstance(current_question, int)
        or not 0 <= current_question < question_count
    ):
        return None

    return {
        "answers": answers,
        "reviewed": reviewed,
        "visited": visited,
        "current_question": current_question,
    }


def _exam_progress_from_record(exam_record):
    return {
        "answers": exam_record["answers"],
        "reviewed": exam_record["reviewed"],
        "visited": exam_record["visited"],
        "current_question": exam_record["current_question"],
    }


def _read_exam_monitoring_stats():
    monitor_file = os.path.join(BASE_DIR, "backend", "monitor_data.json")
    try:
        with open(monitor_file, "r", encoding="utf-8") as monitor:
            data = json.load(monitor)
        return int(data.get("faces", 0)), int(data.get("violations", 0))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        app.logger.exception("Unable to read exam monitoring data.")
        return 0, 0


def _read_monitoring_data():
    monitor_file = os.path.join(BASE_DIR, "backend", "monitor_data.json")
    with _monitor_stats_lock:
        try:
            with open(monitor_file, "r", encoding="utf-8") as monitor:
                data = json.load(monitor)
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            app.logger.exception("Unable to read exam monitoring data.")
            raise RuntimeError("Exam monitoring data is unavailable.") from exc

    if not isinstance(data, dict):
        raise RuntimeError("Exam monitoring data has an invalid format.")
    return data


def _write_monitoring_data(exam_id, faces=None, violations=None, reset=False):
    monitor_file = os.path.join(BASE_DIR, "backend", "monitor_data.json")
    with _monitor_stats_lock:
        if reset:
            data = {"exam_id": exam_id, "faces": 0, "violations": 0}
        else:
            try:
                with open(monitor_file, "r", encoding="utf-8") as monitor:
                    data = json.load(monitor)
            except (OSError, json.JSONDecodeError) as exc:
                app.logger.exception("Unable to update exam monitoring data.")
                raise RuntimeError("Exam monitoring data is unavailable.") from exc

            if not isinstance(data, dict) or data.get("exam_id") != exam_id:
                return False
            if faces is not None:
                data["faces"] = faces
            if violations is not None:
                data["violations"] = max(
                    int(data.get("violations", 0)),
                    violations,
                )

        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=os.path.dirname(monitor_file),
                delete=False,
            ) as monitor:
                temporary_path = monitor.name
                json.dump(data, monitor)
                monitor.flush()
                os.fsync(monitor.fileno())
            os.replace(temporary_path, monitor_file)
            temporary_path = None
        finally:
            if temporary_path and os.path.exists(temporary_path):
                os.remove(temporary_path)
    return True


def _reset_exam_monitoring_stats(exam_id):
    _write_monitoring_data(exam_id, reset=True)


def _ensure_exam_monitoring_stats(exam_id):
    data = _read_monitoring_data()
    if data and data.get("exam_id") == exam_id:
        return
    _reset_exam_monitoring_stats(exam_id)


def _count_exam_evidence():
    evidence_folder = os.path.join(BASE_DIR, "backend", "evidence")
    if not os.path.isdir(evidence_folder):
        return 0
    return sum(
        1
        for root, _, files in os.walk(evidence_folder)
        for filename in files
        if filename.lower().endswith((".jpg", ".jpeg", ".png"))
    )


def _complete_exam(exam_record, progress):
    faces, violations = _read_exam_monitoring_stats()
    evidence_count = _count_exam_evidence()
    completed = complete_exam_session(
        exam_record["exam_id"],
        progress["answers"],
        progress["reviewed"],
        progress["visited"],
        progress["current_question"],
        faces,
        violations,
        evidence_count,
    )
    if completed is None:
        raise RuntimeError(
            f"Exam session {exam_record['exam_id']} disappeared during submission."
        )
    if completed["newly_submitted"] and supabase is not None:
        try:
            supabase.table("exam_reports").insert({
                "candidate_name": completed["candidate_name"],
                "face_count": completed["face_count"],
                "violations": completed["violations"],
                "evidence_count": completed["evidence_count"],
            }).execute()
        except Exception:
            app.logger.exception(
                "Submitted exam %s could not be synced to Supabase.",
                completed["exam_id"],
            )
    return completed


def _exam_session_response(exam_record, resume_token, progress=None):
    return {
        "status": exam_record["status"],
        "exam_id": exam_record["exam_id"],
        "resume_token": resume_token,
        "questions": _public_exam_questions(exam_record),
        "answers": (
            progress["answers"] if progress else exam_record["answers"]
        ),
        "reviewed": (
            progress["reviewed"] if progress else exam_record["reviewed"]
        ),
        "visited": (
            progress["visited"] if progress else exam_record["visited"]
        ),
        "current_question": (
            progress["current_question"]
            if progress
            else exam_record["current_question"]
        ),
        "started_at": exam_record["started_at"],
        "deadline_at": exam_record["deadline_at"],
        "last_saved_at": exam_record["last_saved_at"],
    }


@app.route("/")
def login():
    return render_template("login.html")


@app.route("/verify", methods=["GET", "POST"])
def verify():
    if request.method == "POST":
        if (
            request.content_length is not None
            and request.content_length > MAX_LOGIN_FRAME_BASE64_LENGTH + 16_384
        ):
            return jsonify({
                "status": "failed",
                "message": "The camera frame is too large. Please restart the camera and try again."
            }), 413
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({
                "status": "failed",
                "message": "Full Name and Registration Number are required."
            }), 400
        name = data.get("name")
        reg_no = data.get("reg_no")
        frame_data = data.get("frame")
    else:
        name = request.args.get("name")
        reg_no = request.args.get("reg_no")
        frame_data = None

    if (
        not isinstance(name, str)
        or not isinstance(reg_no, str)
        or not name.strip()
        or not reg_no.strip()
        or len(name.strip()) > 120
        or len(reg_no.strip()) > 64
    ):
        return jsonify({
            "status": "failed",
            "message": "Enter a valid Full Name and Registration Number."
        }), 400

    name = name.strip()
    reg_no = reg_no.strip()

    session.pop("candidate_name", None)
    session.pop("reg_no", None)
    session.pop("active_exam_id", None)
    app.logger.info("Candidate face verification attempt received.")

    if not isinstance(frame_data, str) or not frame_data:
        return jsonify({
            "status": "failed",
            "message": "Start the camera and capture a frame before verifying."
        }), 400

    try:
        captured_frame = _decode_candidate_camera_frame(frame_data)
    except ValueError:
        app.logger.info("Candidate verification received an invalid browser camera frame.")
        return jsonify({
            "status": "failed",
            "message": "The camera frame could not be read. Restart the camera and try again."
        }), 400

    try:
        if not hasattr(cv2, "CascadeClassifier") or not hasattr(cv2, "data"):
            app.logger.error("OpenCV face detector is unavailable.")
            return jsonify({
                "status": "failed",
                "message": "Face detection is unavailable. Please contact the exam administrator."
            }), 503

        face_cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        if face_cascade.empty():
            app.logger.error("OpenCV could not load the face detection model.")
            return jsonify({
                "status": "failed",
                "message": "Face detection could not start. Please contact the exam administrator."
            }), 503

        gray = cv2.cvtColor(captured_frame, cv2.COLOR_BGR2GRAY)
        faces = face_cascade.detectMultiScale(
            gray,
            scaleFactor=1.2,
            minNeighbors=4
        )
    except Exception:
        app.logger.exception("Candidate face verification failed.")
        return jsonify({
            "status": "failed",
            "message": "Face detection could not be completed. Please try again or contact the exam administrator."
        }), 503

    if len(faces) > 1:
        return jsonify({
            "status": "failed",
            "message": "Only one face may be visible during verification."
        }), 403
    if len(faces) == 0:
        return jsonify({
            "status": "failed",
            "message": "Face not detected in webcam! Please sit directly in front of the camera with good lighting."
        }), 403

    reference_path = _candidate_face_reference_path(reg_no)
    registration_key = _candidate_registration_key(reg_no)
    enrolled_now = False
    candidate_name = name

    if supabase is not None:
        try:
            cloud_profile = _get_supabase_candidate_profile(registration_key)
        except Exception:
            app.logger.exception("Could not load the candidate profile from Supabase.")
            return jsonify({
                "status": "failed",
                "message": "Cloud candidate profiles are not ready. Run database/supabase_candidate_profiles.sql in the Supabase SQL Editor, then try again."
            }), 503

        if cloud_profile:
            stored_name = cloud_profile.get("candidate_name")
            if not isinstance(stored_name, str) or not _same_candidate_name(
                name,
                stored_name,
            ):
                return jsonify({
                    "status": "failed",
                    "message": "The name does not match the profile registered for this Registration Number."
                }), 403

            try:
                _download_supabase_face(
                    cloud_profile,
                    registration_key,
                    reference_path,
                )
                if not _face_matches_reference(captured_frame, reference_path):
                    app.logger.warning(
                        "Face mismatch for registration number ending in %s.",
                        reg_no[-4:],
                    )
                    return jsonify({
                        "status": "failed",
                        "message": "This face does not match the face enrolled for this Registration Number."
                    }), 403
            except ImportError:
                app.logger.exception("The face recognition package is unavailable.")
                return jsonify({
                    "status": "failed",
                    "message": "Face identity matching is unavailable. Please contact the exam administrator."
                }), 503
            except Exception:
                app.logger.exception(
                    "Cloud face identity matching failed for registration number ending in %s.",
                    reg_no[-4:],
                )
                return jsonify({
                    "status": "failed",
                    "message": "The saved face could not be loaded or matched. Check the Supabase candidate-faces bucket and try again."
                }), 503
            candidate_name = stored_name
        else:
            try:
                if os.path.exists(reference_path):
                    app.logger.info(
                        "Replacing an untrusted local face reference because no Supabase profile exists for registration ending in %s.",
                        reg_no[-4:],
                    )
                _save_local_face_reference(
                    captured_frame,
                    reference_path,
                    replace_existing=True,
                )
                enrolled_now = True
            except Exception:
                app.logger.exception("Candidate face enrollment failed.")
                return jsonify({
                    "status": "failed",
                    "message": "The face could not be enrolled. Please try again or contact the exam administrator."
                }), 503

            try:
                cloud_profile, created = _save_candidate_profile_to_supabase(
                    name,
                    reg_no,
                    registration_key,
                    reference_path,
                )
                enrolled_now = created
            except Exception:
                app.logger.exception("Could not save the candidate profile to Supabase.")
                return jsonify({
                    "status": "failed",
                    "message": "The candidate profile could not be saved to Supabase. Check the SQL setup, server key, and candidate-faces bucket."
                }), 503

            if not created:
                stored_name = cloud_profile.get("candidate_name")
                if not isinstance(stored_name, str) or not _same_candidate_name(
                    name,
                    stored_name,
                ):
                    return jsonify({
                        "status": "failed",
                        "message": "The name does not match the profile registered for this Registration Number."
                    }), 403
                try:
                    _download_supabase_face(
                        cloud_profile,
                        registration_key,
                        reference_path,
                    )
                    if not _face_matches_reference(captured_frame, reference_path):
                        return jsonify({
                            "status": "failed",
                            "message": "This face does not match the face enrolled for this Registration Number."
                        }), 403
                except Exception:
                    app.logger.exception("Concurrent cloud face enrollment could not be verified.")
                    return jsonify({
                        "status": "failed",
                        "message": "The saved face could not be loaded or matched. Please try again or contact the exam administrator."
                    }), 503
                candidate_name = stored_name
    else:
        if os.path.exists(reference_path):
            try:
                if not _face_matches_reference(captured_frame, reference_path):
                    app.logger.warning(
                        "Face mismatch for registration number ending in %s.",
                        reg_no[-4:],
                    )
                    return jsonify({
                        "status": "failed",
                        "message": "This face does not match the face enrolled for this Registration Number."
                    }), 403
            except ImportError:
                app.logger.exception("The face recognition package is unavailable.")
                return jsonify({
                    "status": "failed",
                    "message": "Face identity matching is unavailable. Please contact the exam administrator."
                }), 503
            except Exception:
                app.logger.exception(
                    "Face identity matching failed for registration number ending in %s.",
                    reg_no[-4:],
                )
                return jsonify({
                    "status": "failed",
                    "message": "Face identity matching could not be completed. Please try again or contact the exam administrator."
                }), 503
        else:
            try:
                os.makedirs(os.path.dirname(reference_path), exist_ok=True)
                encoded, image_data = cv2.imencode(".jpg", captured_frame)
                if not encoded:
                    raise OSError("Could not encode the enrolled face image.")
                with open(reference_path, "xb") as reference_file:
                    reference_file.write(bytes(image_data))
                    reference_file.flush()
                    os.fsync(reference_file.fileno())
                enrolled_now = True
            except FileExistsError:
                try:
                    if not _face_matches_reference(captured_frame, reference_path):
                        return jsonify({
                            "status": "failed",
                            "message": "This face does not match the face enrolled for this Registration Number."
                        }), 403
                except Exception:
                    app.logger.exception("Concurrent face enrollment could not be verified.")
                    return jsonify({
                        "status": "failed",
                        "message": "Face identity matching could not be completed. Please try again or contact the exam administrator."
                    }), 503
            except OSError:
                app.logger.exception("Could not save the candidate face enrollment.")
                return jsonify({
                    "status": "failed",
                    "message": "The face could not be enrolled. Please try again or contact the exam administrator."
                }), 503

    session["candidate_name"] = candidate_name
    session["reg_no"] = reg_no
    session.permanent = True

    return jsonify({
        "status": "success",
        "message": (
            f"Welcome {candidate_name}! Your face is enrolled for this Registration Number."
            if enrolled_now
            else f"Welcome {candidate_name}! Your face matched the enrolled profile."
        )
    })


@app.route("/instructions")
def instructions():
    return render_template("instructions.html")


@app.route("/exam")
def exam():
    return render_template("exam.html")


@app.route("/api/exam/readiness", methods=["POST"])
def exam_readiness():
    if not session.get("candidate_name") or not session.get("reg_no"):
        return jsonify({
            "error": "Sign in as a candidate before starting an exam.",
            "check": "candidate_session",
        }), 401

    try:
        questions = get_all_questions()
    except Exception:
        app.logger.exception("Could not load the question bank for the readiness check.")
        return jsonify({
            "error": "The question bank could not be checked. Please try again.",
            "check": "questions",
        }), 503

    exam_questions = _prepare_exam_questions(questions)
    if not exam_questions:
        app.logger.error("The question bank is empty or contains invalid questions.")
        return jsonify({
            "error": "No valid exam questions are available. Contact the administrator.",
            "check": "questions",
        }), 503

    probe_exam_id = f"readiness-{secrets.token_urlsafe(18)}"
    probe_token_hash = _hash_resume_token(secrets.token_urlsafe(32))
    now = time.time()
    probe_questions = [{
        "id": "readiness-probe",
        "question": "Readiness check",
        "options": ["A", "B", "C", "D"],
    }]
    probe_conn = None
    try:
        probe_conn = sqlite3.connect(DB_NAME, timeout=5)
        probe_conn.execute("BEGIN IMMEDIATE")
        probe_conn.execute(
            """
            INSERT INTO exam_sessions(
                exam_id, resume_token_hash, candidate_name, reg_no,
                questions_json, answers_json, reviewed_json, visited_json,
                current_question, started_at, deadline_at, last_saved_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                probe_exam_id,
                probe_token_hash,
                "readiness-check",
                "readiness-check",
                json.dumps(probe_questions),
                json.dumps([None]),
                json.dumps([False]),
                json.dumps([True]),
                now,
                now + 60,
                now,
            ),
        )
        saved = probe_conn.execute(
            """
            UPDATE exam_sessions
            SET answers_json = ?, reviewed_json = ?, visited_json = ?,
                current_question = ?, last_saved_at = ?
            WHERE exam_id = ? AND status = 'in_progress' AND deadline_at > ?
            """,
            (
                json.dumps([0]),
                json.dumps([False]),
                json.dumps([True]),
                0,
                now,
                probe_exam_id,
                now,
            ),
        )
        if saved.rowcount != 1:
            raise sqlite3.OperationalError("The autosave readiness probe did not persist.")
        probe_conn.rollback()
    except sqlite3.Error:
        app.logger.exception("The exam autosave readiness check failed.")
        return jsonify({
            "error": "Answer autosave is not ready. Please retry or contact the administrator.",
            "check": "autosave",
            "question_count": len(questions),
        }), 503
    finally:
        if probe_conn is not None:
            probe_conn.close()

    return jsonify({
        "status": "ready",
        "checks": {
            "candidate_session": True,
            "questions": True,
            "question_count": len(exam_questions),
            "autosave": True,
        },
    })


@app.route("/api/exam/readiness/face", methods=["POST"])
def exam_readiness_face():
    if not session.get("candidate_name") or not session.get("reg_no"):
        return jsonify({"error": "Sign in as a candidate before starting an exam."}), 401
    if request.content_length and request.content_length > 1024 * 1024:
        return jsonify({"error": "The camera frame is too large. Please retry."}), 413

    image = request.files.get("frame")
    if image is None or image.mimetype != "image/jpeg":
        return jsonify({"error": "A JPEG camera frame is required."}), 400

    image_data = image.read(1024 * 1024 + 1)
    if not image_data or len(image_data) > 1024 * 1024:
        return jsonify({"error": "The camera frame is empty or too large."}), 400

    frame = cv2.imdecode(
        np.frombuffer(image_data, dtype=np.uint8),
        cv2.IMREAD_COLOR,
    )
    if frame is None:
        return jsonify({"error": "The camera frame could not be read. Please retry."}), 400

    try:
        face_cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        if face_cascade.empty():
            raise OSError("OpenCV could not load its face detector.")
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = face_cascade.detectMultiScale(
            gray,
            scaleFactor=1.2,
            minNeighbors=4,
            minSize=(60, 60),
        )
    except Exception:
        app.logger.exception("The pre-exam face-detection check failed.")
        return jsonify({
            "error": "Face detection is unavailable. Please retry or contact the administrator."
        }), 503

    return jsonify({"status": "ok", "faces": len(faces)})


@app.route("/api/exam/session", methods=["POST"])
def exam_session():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "A JSON request is required"}), 400

    resume_token = data.get("resume_token")
    if resume_token is not None and (
        not isinstance(resume_token, str) or len(resume_token) > 128
    ):
        return jsonify({"error": "The saved exam token is invalid"}), 400

    exam_record = None
    if resume_token:
        exam_record = get_exam_session_by_token_hash(
            _hash_resume_token(resume_token)
        )
        if exam_record and session.get("candidate_name"):
            same_candidate = (
                hmac.compare_digest(
                    exam_record["candidate_name"],
                    session["candidate_name"]
                )
                and hmac.compare_digest(
                    exam_record["reg_no"],
                    session.get("reg_no", "")
                )
            )
            if not same_candidate:
                exam_record = None

    if exam_record is None and not resume_token:
        active_exam_id = session.get("active_exam_id")
        if active_exam_id:
            exam_record = get_exam_session(active_exam_id)
            if exam_record and session.get("candidate_name"):
                if (
                    exam_record["candidate_name"] != session["candidate_name"]
                    or exam_record["reg_no"] != session.get("reg_no", "")
                ):
                    exam_record = None
            if exam_record and exam_record["status"] == "submitted":
                app.logger.info(
                    "Starting a new exam attempt after submitted exam %s.",
                    exam_record["exam_id"],
                )
                exam_record = None

    if exam_record is None and resume_token and not session.get("candidate_name"):
        return jsonify({
            "error": "The saved exam could not be found. Sign in again to resume it."
        }), 404

    if exam_record is None:
        candidate_name = session.get("candidate_name")
        if not candidate_name:
            return jsonify({
                "error": "Sign in as a candidate before starting an exam."
            }), 401

        try:
            exam_questions = _prepare_exam_questions(get_all_questions())
        except Exception:
            app.logger.exception("Could not load the exam question bank.")
            return jsonify({
                "error": "The question bank could not be loaded. Please try again or contact the administrator."
            }), 503
        if not exam_questions:
            app.logger.error("The question bank contains an invalid exam question.")
            return jsonify({
                "error": "The exam question bank is invalid. Contact the administrator."
            }), 503

        resume_token = secrets.token_urlsafe(32)
        exam_id = secrets.token_urlsafe(24)
        try:
            _reset_exam_monitoring_stats(exam_id)
        except OSError:
            app.logger.exception("Could not reset monitoring statistics for a new exam.")
            return jsonify({
                "error": "Exam monitoring could not be initialized. Please try again or contact the administrator."
            }), 503
        create_exam_session(
            exam_id,
            _hash_resume_token(resume_token),
            candidate_name,
            session.get("reg_no", ""),
            exam_questions,
            duration_seconds=60 * 60,
        )
        exam_record = get_exam_session(exam_id)
    elif not resume_token:
        resume_token = secrets.token_urlsafe(32)
        if not rotate_exam_resume_token(
            exam_record["exam_id"],
            _hash_resume_token(resume_token)
        ):
            app.logger.error(
                "Could not rotate resume token for exam %s.",
                exam_record["exam_id"]
            )
            return jsonify({"error": "Could not secure the exam session"}), 500

    _set_exam_session(exam_record)
    if exam_record["status"] == "in_progress":
        try:
            _ensure_exam_monitoring_stats(exam_record["exam_id"])
        except (OSError, RuntimeError):
            app.logger.exception(
                "Could not initialize monitoring for exam %s.",
                exam_record["exam_id"],
            )
            return jsonify({
                "error": "Exam monitoring could not be initialized. Please try again or contact the administrator."
            }), 503

    if exam_record["status"] == "submitted":
        response_data = _exam_session_response(exam_record, resume_token)
        response_data["submission_reason"] = "already_submitted"
        return jsonify(response_data)

    if exam_record["deadline_at"] <= time.time():
        completed = _complete_exam(
            exam_record,
            _exam_progress_from_record(exam_record)
        )
        response_data = _exam_session_response(completed, resume_token)
        response_data["submission_reason"] = "time_expired"
        return jsonify(response_data)

    response_data = _exam_session_response(exam_record, resume_token)
    return jsonify(response_data)


def _resolve_exam_request(data):
    resume_token = data.get("resume_token")
    if not isinstance(resume_token, str) or not resume_token or len(resume_token) > 128:
        return None, None

    exam_record = get_exam_session_by_token_hash(
        _hash_resume_token(resume_token)
    )
    if exam_record is None:
        return None, resume_token

    current_candidate = session.get("candidate_name")
    if current_candidate and (
        not hmac.compare_digest(exam_record["candidate_name"], current_candidate)
        or not hmac.compare_digest(
            exam_record["reg_no"],
            session.get("reg_no", "")
        )
    ):
        return None, resume_token

    _set_exam_session(exam_record)
    return exam_record, resume_token


@app.route("/api/exam/autosave", methods=["POST"])
def autosave_exam():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "A JSON request is required"}), 400

    exam_record, resume_token = _resolve_exam_request(data)
    if exam_record is None:
        return jsonify({"error": "The exam session could not be restored"}), 401

    if exam_record["status"] == "submitted":
        return jsonify({"status": "submitted"})

    progress = _validated_exam_progress(data, exam_record)
    if progress is None:
        return jsonify({"error": "The autosaved exam progress is invalid"}), 400

    if exam_record["deadline_at"] <= time.time():
        completed = _complete_exam(exam_record, progress)
        return jsonify({
            "status": "submitted",
            "submission_reason": "time_expired",
            "last_saved_at": completed["last_saved_at"],
        })

    if not save_exam_progress(
        exam_record["exam_id"],
        progress["answers"],
        progress["reviewed"],
        progress["visited"],
        progress["current_question"],
    ):
        latest = get_exam_session(exam_record["exam_id"])
        if latest and latest["status"] == "submitted":
            return jsonify({"status": "submitted"})
        if latest and latest["deadline_at"] <= time.time():
            _complete_exam(latest, progress)
            return jsonify({
                "status": "submitted",
                "submission_reason": "time_expired",
            })
        return jsonify({"error": "Exam progress could not be saved"}), 500

    return jsonify({"status": "saved", "last_saved_at": time.time()})


@app.route("/api/exam/submit", methods=["POST"])
def submit_exam_session():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "A JSON request is required"}), 400

    exam_record, resume_token = _resolve_exam_request(data)
    if exam_record is None:
        return jsonify({"error": "The exam session could not be restored"}), 401
    if exam_record["status"] == "submitted":
        return jsonify({"status": "submitted"})

    progress = _validated_exam_progress(data, exam_record)
    if progress is None:
        return jsonify({"error": "The final exam progress is invalid"}), 400

    reason = data.get("reason", "manual")
    if reason not in {"manual", "time_expired", "violation_limit"}:
        return jsonify({"error": "The exam submission reason is invalid"}), 400
    if reason == "time_expired" and exam_record["deadline_at"] > time.time():
        return jsonify({
            "error": "The exam time has not expired yet."
        }), 409

    if exam_record["deadline_at"] <= time.time():
        reason = "time_expired"

    completed = _complete_exam(exam_record, progress)
    return jsonify({
        "status": "submitted",
        "submission_reason": reason,
        "last_saved_at": completed["last_saved_at"],
    })


@app.route("/result")
def result():

    exam_record = get_exam_session(session.get("active_exam_id", ""))
    if exam_record and exam_record["status"] == "in_progress":
        if exam_record["deadline_at"] > time.time():
            return redirect(url_for("exam"))
        exam_record = _complete_exam(
            exam_record,
            _exam_progress_from_record(exam_record)
        )

    if exam_record and exam_record["status"] == "submitted":
        faces = exam_record["face_count"] or 0
        violations = exam_record["violations"] or 0
        evidence_count = exam_record["evidence_count"] or 0
    else:
        faces, violations = _read_exam_monitoring_stats()
        evidence_count = _count_exam_evidence()

 #   save_report(
  # "Srijan Akshit",
  # faces,
  #  violations,
  #  evidence_count
#)
   
    candidate_name = session.get("candidate_name", "Candidate")

    return render_template(
        "result.html",
        candidate_name=candidate_name,
        faces=faces,
        violations=violations,
        evidence_count=evidence_count
    )
@app.route("/submit_exam")
def submit_exam():
    exam_record = get_exam_session(session.get("active_exam_id", ""))
    if exam_record and exam_record["status"] == "in_progress":
        _complete_exam(
            exam_record,
            _exam_progress_from_record(exam_record)
        )
    return redirect("/result")


@app.route("/monitor")
def monitor():
    exam_id = session.get("active_exam_id")
    if not exam_id:
        return jsonify({"faces": 0, "violations": 0})
    try:
        data = _read_monitoring_data()
    except RuntimeError:
        return jsonify({"error": "Exam monitoring data is unavailable."}), 503
    if not data or data.get("exam_id") != exam_id:
        return jsonify({"faces": 0, "violations": 0})
    return jsonify({
        "faces": int(data.get("faces", 0)),
        "violations": int(data.get("violations", 0)),
    })

@app.route("/update_violation", methods=["POST"])
def update_violation():
    data = request.get_json(silent=True)
    violations = data.get("violations") if isinstance(data, dict) else None
    if (
        isinstance(violations, bool)
        or not isinstance(violations, int)
        or violations < 0
    ):
        return jsonify({"error": "A valid violation count is required."}), 400

    exam_id = session.get("active_exam_id")
    if not exam_id:
        return jsonify({"error": "An active exam session is required."}), 401

    exam_record = get_exam_session(exam_id)
    if not exam_record or exam_record["status"] != "in_progress":
        return jsonify({"error": "The exam session is no longer active."}), 409

    try:
        updated = _write_monitoring_data(
            exam_id,
            violations=violations,
        )
    except RuntimeError:
        return jsonify({"error": "Exam monitoring data is unavailable."}), 503
    if not updated:
        return jsonify({"error": "The exam monitoring session has changed."}), 409
    return jsonify({"status": "ok"})

@app.route("/video_feed")
def video_feed():
    exam_id = request.args.get("exam_id", "")
    if not exam_id or exam_id != session.get("active_exam_id"):
        return jsonify({"error": "An active exam session is required."}), 409

    exam_record = get_exam_session(exam_id)
    if not exam_record or exam_record["status"] != "in_progress":
        return jsonify({"error": "The exam session is no longer active."}), 409

    try:
        monitor_data = _read_monitoring_data()
    except RuntimeError:
        return jsonify({"error": "Exam monitoring data is unavailable."}), 503
    if not monitor_data or monitor_data.get("exam_id") != exam_id:
        return jsonify({"error": "The exam monitoring session has changed."}), 409

    def generate():

        cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap = cv2.VideoCapture(0)

        face_cascade = None
        try:
            if hasattr(cv2, "CascadeClassifier") and hasattr(cv2, "data"):
                face_cascade = cv2.CascadeClassifier(
                    cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
                )
        except Exception:
            face_cascade = None

        evidence_no_face = os.path.join(BASE_DIR, "backend", "evidence", "no_face")
        evidence_multi = os.path.join(BASE_DIR, "backend", "evidence", "multiple_faces")
        os.makedirs(evidence_no_face, exist_ok=True)
        os.makedirs(evidence_multi, exist_ok=True)

        no_face_start = None
        multi_face_start = None
        last_evidence_time = 0
        last_json_save = 0

        violations = int(monitor_data.get("violations", 0))

        while True:

            success, frame = cap.read()

            if not success:
                break

            faces = []
            if face_cascade is not None:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                faces = face_cascade.detectMultiScale(
                    gray, scaleFactor=1.15, minNeighbors=5, minSize=(60, 60)
                )

            face_count = len(faces)
            now = time.time()

            if face_count == 1:
                no_face_start = None
                multi_face_start = None
                (x, y, w, h) = faces[0]
                cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 0), 2)
                cv2.putText(frame, "Candidate", (x, max(y - 10, 20)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
                cv2.putText(frame, "Status: Verified", (20, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

            elif face_count == 0:
                multi_face_start = None
                if no_face_start is None:
                    no_face_start = now
                elapsed = now - no_face_start
                cv2.putText(frame, f"WARNING: NO FACE ({int(elapsed)}s)", (20, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 165, 255), 2)
                if elapsed >= 5 and (now - last_evidence_time > 5):
                    violations += 1
                    last_evidence_time = now
                    ts = time.strftime("%Y%m%d_%H%M%S")
                    evidence_path = os.path.join(
                        evidence_no_face,
                        f"no_face_{ts}.jpg"
                    )
                    if not cv2.imwrite(evidence_path, frame):
                        raise OSError(
                            f"Could not save evidence image: {evidence_path}"
                        )
                    append_violation_log("No Face Detected", evidence_path)
                    _store_evidence_review_event(
                        exam_id,
                        "No Face Detected",
                        evidence_path,
                        now,
                    )
                    if supabase is not None:
                        try:
                            _, ev_bytes = cv2.imencode(".jpg", frame)
                            supabase.storage.from_("evidence").upload(
                                path=f"no_face/no_face_{ts}.jpg",
                                file=ev_bytes.tobytes(),
                                file_options={"content-type": "image/jpeg", "upsert": "true"}
                            )
                        except Exception:
                            pass

            else:
                no_face_start = None
                for (x, y, w, h) in faces:
                    cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 0, 255), 2)
                if multi_face_start is None:
                    multi_face_start = now
                elapsed = now - multi_face_start
                cv2.putText(frame, f"ALERT: MULTIPLE FACES ({face_count})", (20, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
                if elapsed >= 2 and (now - last_evidence_time > 5):
                    violations += 1
                    last_evidence_time = now
                    ts = time.strftime("%Y%m%d_%H%M%S")
                    evidence_path = os.path.join(
                        evidence_multi,
                        f"multi_{ts}.jpg"
                    )
                    if not cv2.imwrite(evidence_path, frame):
                        raise OSError(
                            f"Could not save evidence image: {evidence_path}"
                        )
                    append_violation_log(
                        "Multiple Faces Detected",
                        evidence_path
                    )
                    _store_evidence_review_event(
                        exam_id,
                        "Multiple Faces Detected",
                        evidence_path,
                        now,
                    )
                    if supabase is not None:
                        try:
                            _, ev_bytes = cv2.imencode(".jpg", frame)
                            supabase.storage.from_("evidence").upload(
                                path=f"multiple_faces/multi_{ts}.jpg",
                                file=ev_bytes.tobytes(),
                                file_options={"content-type": "image/jpeg", "upsert": "true"}
                            )
                        except Exception:
                            pass

            cv2.putText(frame, f"Violations: {violations}", (20, frame.shape[0] - 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)

            if now - last_json_save >= 1:
                last_json_save = now
                try:
                    updated = _write_monitoring_data(
                        exam_id,
                        faces=face_count,
                        violations=violations,
                    )
                except RuntimeError:
                    app.logger.exception(
                        "Could not update monitoring data for exam %s.",
                        exam_id,
                    )
                    break
                if not updated:
                    break
                saved = _read_monitoring_data()
                if not saved or saved.get("exam_id") != exam_id:
                    break
                violations = int(saved.get("violations", 0))

            _, buffer = cv2.imencode(".jpg", frame)

            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n"
                + buffer.tobytes()
                + b"\r\n"
            )

        cap.release()

    return Response(
        generate(),
        mimetype="multipart/x-mixed-replace; boundary=frame"
    )
@app.route("/evidence")
@admin_required
def evidence():

        evidence_folder = os.path.join(
            BASE_DIR,
            "backend",
            "evidence"
        )
        print("BASE_DIR =", BASE_DIR)
        print("EVIDENCE FOLDER =", evidence_folder)
        print("EXISTS =", os.path.exists(evidence_folder))
        
        images = []

        for root, dirs, files in os.walk(evidence_folder):

         for file in files:

            if file.lower().endswith((".jpg", ".jpeg", ".png")):

                rel_path = os.path.relpath(
                    os.path.join(root, file),
                    evidence_folder
                ).replace("\\", "/")

                images.append(rel_path)

        images = images[-3:]

        print("TOTAL IMAGES =", len(images))
        print(images)

        return render_template(
        "evidence.html",
        images=images
    )
@app.route("/evidence/<path:filename>")
@admin_required
def evidence_image(filename):

    evidence_folder = os.path.join(
        BASE_DIR,
        "backend",
        "evidence"
    )

    clean_filename = filename.replace("\\", "/")
    return send_from_directory(
        evidence_folder,
        clean_filename
    )
@app.route("/download_report")
def download_report():

    candidate_name = session.get("candidate_name", "Candidate")
    exam_record = get_exam_session(session.get("active_exam_id", ""))
    if exam_record and exam_record["status"] == "submitted":
        candidate_name = exam_record["candidate_name"]
        faces = exam_record["face_count"] or 0
        violations = exam_record["violations"] or 0
        evidence_count = exam_record["evidence_count"] or 0
    else:
        faces, violations = _read_exam_monitoring_stats()
        evidence_count = _count_exam_evidence()

    pdf_file = os.path.join(BASE_DIR, "exam_report.pdf")

    doc = SimpleDocTemplate(pdf_file)

    styles = getSampleStyleSheet()

    content = [

        Paragraph("ExamGuard AI Monitoring Report",
                  styles["Title"]),

        Spacer(1, 20),

        Paragraph(f"Candidate: {candidate_name}",
                  styles["Normal"]),

        Paragraph(f"Final Face Count: {faces}",
                  styles["Normal"]),

        Paragraph(f"Total Violations: {violations}",
                  styles["Normal"]),

        Paragraph(f"Evidence Captured: {evidence_count}",
                  styles["Normal"]),

        Spacer(1, 20),

        Paragraph("Report Generated Successfully",
                  styles["Heading2"])

    ]
    print("PDF Faces =", faces)
    print("PDF Violations =", violations)
    print("PDF Evidence =", evidence_count)
    doc.build(content)

    return send_file(
        pdf_file,
        as_attachment=True
    )
    print("ADMIN ROUTE LOADED")

@app.route("/api/questions", methods=["GET"])
def api_get_questions():
    questions = get_all_questions()
    return jsonify([
        {
            "id": question["id"],
            "question": question["question"],
            "options": question["options"]
        }
        for question in questions
    ])


@app.route("/api/questions/add", methods=["POST"])
@admin_required
@admin_csrf_required
def api_add_question():
    if request.is_json:
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({
                "status": "failed",
                "message": "A JSON object with question fields is required"
            }), 400
    else:
        data = request.form

    q_text = data.get("question", "").strip()
    opt_a = data.get("option_a", "").strip()
    opt_b = data.get("option_b", "").strip()
    opt_c = data.get("option_c", "").strip()
    opt_d = data.get("option_d", "").strip()
    try:
        correct = int(data.get("correct", 0))
    except (ValueError, TypeError):
        if request.is_json:
            return jsonify({
                "status": "failed",
                "message": "Correct answer must be an integer from 0 to 3"
            }), 400
        return "Correct answer must be selected from options A through D.", 400

    if not 0 <= correct <= 3:
        if request.is_json:
            return jsonify({
                "status": "failed",
                "message": "Correct answer must be between 0 and 3"
            }), 400
        return "Correct answer must be between 0 and 3.", 400

    if not q_text or not opt_a or not opt_b or not opt_c or not opt_d:
        if request.is_json:
            return jsonify({
                "status": "failed",
                "message": "Question and all four options are required"
            }), 400
        return "Question and all four options are required.", 400

    add_question(q_text, opt_a, opt_b, opt_c, opt_d, correct)
    if request.is_json:
        return jsonify({"status": "success", "message": "Question added"})
    return redirect("/admin")


@app.route("/api/questions/delete/<int:qid>", methods=["POST"])
@admin_required
@admin_csrf_required
def api_delete_question(qid):
    delete_question(qid)
    if request.is_json:
        return jsonify({"status": "success"})
    return redirect("/admin")


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if session.get("is_admin"):
        return redirect(url_for("admin"))

    configured_username = os.getenv("ADMIN_USERNAME", "").strip()
    configured_password = os.getenv("ADMIN_PASSWORD", "")
    configuration_error = None
    error = None

    if not configured_username or not configured_password:
        configuration_error = (
            "Admin sign-in is not configured. Set ADMIN_USERNAME and ADMIN_PASSWORD "
            "in the project .env file, then restart ExamGuard."
        )
        if request.method == "POST":
            return render_template(
                "admin_login.html",
                error=configuration_error
            ), 503
    elif request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if (
            hmac.compare_digest(username, configured_username)
            and hmac.compare_digest(password, configured_password)
        ):
            session.clear()
            session["is_admin"] = True
            session["admin_username"] = configured_username
            session["admin_csrf_token"] = secrets.token_urlsafe(32)
            return redirect(url_for("admin"))
        error = "The username or password is incorrect."

    return render_template(
        "admin_login.html",
        error=configuration_error or error
    )


@app.route("/admin/logout", methods=["POST"])
@admin_required
@admin_csrf_required
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


@app.route("/admin")
@admin_required
def admin():
    reports = get_all_reports()
    questions = get_all_questions()
    violation_counts = [int(report[3] or 0) for report in reports]
    violations_total = sum(violation_counts)
    low_risk_reports = sum(1 for count in violation_counts if count <= 1)
    medium_risk_reports = sum(
        1 for count in violation_counts if 1 < count <= 4
    )
    high_risk_reports = sum(1 for count in violation_counts if count > 4)
    chart_reports = list(reversed(reports))

    return render_template(
        "admin_portal.html",
        reports=reports,
        chart_labels=[report[1] or "Candidate" for report in chart_reports],
        chart_values=[int(report[3] or 0) for report in chart_reports],
        questions=questions,
        violations_total=violations_total,
        low_risk_reports=low_risk_reports,
        medium_risk_reports=medium_risk_reports,
        high_risk_reports=high_risk_reports,
        csrf_token=session["admin_csrf_token"],
        admin_username=session.get("admin_username", "Admin")
    )


@app.route("/admin/reports")
@admin_required
def admin_reports():
    try:
        filters = _requested_report_filters()
        reports = get_admin_reports(filters)
    except ValueError as error:
        return str(error), 400
    except sqlite3.Error:
        app.logger.exception("Could not load filtered admin reports.")
        return "Reports are temporarily unavailable. Please retry.", 503

    return render_template(
        "admin_reports.html",
        reports=reports,
        filters=filters,
        csrf_token=session["admin_csrf_token"],
        admin_username=session.get("admin_username", "Admin"),
    )


def _filtered_admin_reports():
    filters = _requested_report_filters()
    return filters, get_admin_reports(filters)


def _csv_safe_cell(value):
    if value is None:
        return ""
    if isinstance(value, str) and value.lstrip(" \t\r\n").startswith(
        ("=", "+", "-", "@")
    ):
        return "'" + value
    return value


@app.route("/admin/reports/export.csv")
@admin_required
def admin_reports_csv():
    try:
        filters, reports = _filtered_admin_reports()
    except ValueError as error:
        return str(error), 400
    except sqlite3.Error:
        app.logger.exception("Could not export filtered admin reports as CSV.")
        return "Reports are temporarily unavailable. Please retry.", 503

    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow([
        "Report ID",
        "Candidate",
        "Exam ID",
        "Completed",
        "Face Count",
        "Violations",
        "Evidence Count",
        "Linked Events",
        "Unreviewed Events",
        "False Positives",
    ])
    for report in reports:
        writer.writerow([
            _csv_safe_cell(report["report_id"]),
            _csv_safe_cell(report["candidate_name"]),
            _csv_safe_cell(report["exam_id"]),
            _csv_safe_cell(report["timestamp"]),
            _csv_safe_cell(report["face_count"]),
            _csv_safe_cell(report["violations"]),
            _csv_safe_cell(report["evidence_count"]),
            _csv_safe_cell(report["linked_event_count"]),
            _csv_safe_cell(report["unreviewed_count"]),
            _csv_safe_cell(report["false_positive_count"]),
        ])

    response = Response(
        "\ufeff" + output.getvalue(),
        mimetype="text/csv; charset=utf-8",
    )
    response.headers["Content-Disposition"] = (
        'attachment; filename="examguard-reports.csv"'
    )
    return response


@app.route("/admin/reports/export.pdf")
@admin_required
def admin_reports_pdf():
    try:
        filters, reports = _filtered_admin_reports()
    except ValueError as error:
        return str(error), 400
    except sqlite3.Error:
        app.logger.exception("Could not export filtered admin reports as PDF.")
        return "Reports are temporarily unavailable. Please retry.", 503

    output = io.BytesIO()
    styles = getSampleStyleSheet()
    table_text = ParagraphStyle(
        "ReportTableText",
        parent=styles["BodyText"],
        fontSize=7,
        leading=9,
    )
    content = [
        Paragraph("ExamGuard AI - Filtered Exam Reports", styles["Title"]),
        Spacer(1, 10),
        Paragraph(
            "Candidate: " + html_escape(filters["candidate"] or "All"),
            styles["BodyText"],
        ),
        Paragraph(
            "Exam ID: " + html_escape(filters["exam_id"] or "All"),
            styles["BodyText"],
        ),
        Paragraph(
            "Date range: "
            + html_escape(filters["date_from"] or "Any")
            + " to "
            + html_escape(filters["date_to"] or "Any"),
            styles["BodyText"],
        ),
        Spacer(1, 14),
    ]

    table_rows = [[
        "Report",
        "Candidate",
        "Exam ID",
        "Completed",
        "Violations",
        "Evidence",
        "Open",
        "False+",
    ]]
    for report in reports:
        table_rows.append([
            str(report["report_id"]),
            Paragraph(
                html_escape(report["candidate_name"] or "Candidate"),
                table_text,
            ),
            Paragraph(html_escape(report["exam_id"] or "Unassigned"), table_text),
            Paragraph(html_escape(str(report["timestamp"] or "")), table_text),
            str(report["violations"] or 0),
            str(report["evidence_count"] or 0),
            str(report["unreviewed_count"] or 0),
            str(report["false_positive_count"] or 0),
        ])
    if len(table_rows) == 1:
        table_rows.append(["", "No reports match these filters.", "", "", "", "", "", ""])

    content.append(LongTable(
        table_rows,
        colWidths=[38, 94, 92, 104, 52, 50, 48, 45],
        repeatRows=1,
        hAlign="LEFT",
    ))
    content[-1].setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#14233d")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 7),
        ("LEADING", (0, 0), (-1, -1), 9),
        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#d8e0eb")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [
            colors.white,
            colors.HexColor("#f5f7fb"),
        ]),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    SimpleDocTemplate(
        output,
        pagesize=letter,
        rightMargin=32,
        leftMargin=32,
        topMargin=36,
        bottomMargin=36,
        title="ExamGuard AI filtered reports",
    ).build(content)

    response = Response(output.getvalue(), mimetype="application/pdf")
    response.headers["Content-Disposition"] = (
        'attachment; filename="examguard-reports.pdf"'
    )
    return response


@app.route("/admin/evidence-review")
@admin_required
def admin_evidence_review():
    evidence_folder = os.path.join(BASE_DIR, "backend", "evidence")
    try:
        for root, _, files in os.walk(evidence_folder):
            for filename in files:
                if not filename.lower().endswith((".jpg", ".jpeg", ".png")):
                    continue
                full_path = os.path.join(root, filename)
                relative_path = os.path.relpath(
                    full_path,
                    evidence_folder,
                ).replace(os.sep, "/")
                event_type = "Captured Evidence"
                first_folder = relative_path.split("/", 1)[0].lower()
                if first_folder == "no_face":
                    event_type = "No Face Detected"
                elif first_folder == "multiple_faces":
                    event_type = "Multiple Faces Detected"
                register_unassigned_evidence(
                    relative_path,
                    event_type,
                    os.path.getmtime(full_path),
                )
        filters = _requested_event_filters()
        events = get_admin_review_events(filters)
    except ValueError as error:
        return str(error), 400
    except (OSError, sqlite3.Error):
        app.logger.exception("Could not load the admin evidence review queue.")
        return "Evidence review is temporarily unavailable. Please retry.", 503

    linked_events = []
    unassigned_events = []
    for event in events:
        event["occurred_at_display"] = datetime.fromtimestamp(
            event["occurred_at"]
        ).strftime("%Y-%m-%d %H:%M:%S")
        if event["reviewed_at"] is not None:
            event["reviewed_at_display"] = datetime.fromtimestamp(
                event["reviewed_at"]
            ).strftime("%Y-%m-%d %H:%M:%S")
        (linked_events if event["exam_id"] else unassigned_events).append(event)

    return render_template(
        "admin_evidence_review.html",
        linked_events=linked_events,
        unassigned_events=unassigned_events,
        filters=filters,
        review_statuses=sorted(REVIEW_DECISIONS),
        csrf_token=session["admin_csrf_token"],
        admin_username=session.get("admin_username", "Admin"),
    )


@app.route("/api/admin/evidence-review/<int:event_id>", methods=["POST"])
@admin_required
@admin_csrf_required
def admin_update_evidence_review(event_id):
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "A JSON review update is required."}), 400
    status = data.get("status")
    note = data.get("note", "")
    if not isinstance(status, str) or status not in REVIEW_DECISIONS:
        return jsonify({"error": "Select a valid review outcome."}), 400
    if not isinstance(note, str) or len(note) > 1000:
        return jsonify({"error": "Review notes must be 1,000 characters or fewer."}), 400

    try:
        reviewed_at = time.time() if status != "unreviewed" else None
        updated = update_evidence_review(
            event_id,
            status,
            note.strip(),
            session.get("admin_username", "Admin"),
        )
    except sqlite3.Error:
        app.logger.exception(
            "Could not save admin review outcome for evidence event %s.",
            event_id,
        )
        return jsonify({"error": "The review could not be saved. Please retry."}), 503
    if not updated:
        return jsonify({"error": "This evidence event no longer exists."}), 404
    return jsonify({
        "status": "saved",
        "reviewed_by": (
            session.get("admin_username", "Admin")
            if reviewed_at is not None
            else None
        ),
        "reviewed_at": reviewed_at,
    })


if __name__ == "__main__":

    print("Templates Path:", os.path.join(BASE_DIR, "templates"))
    print("Static Path:", os.path.join(BASE_DIR, "static"))
    print(app.url_map)
    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False
    )