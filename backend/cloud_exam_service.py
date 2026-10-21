"""Isolated helpers for the future Supabase-backed multi-candidate exam flow."""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import os
import posixpath
import re
import secrets
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from xml.etree import ElementTree


MAX_ROSTER_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_ROSTER_CANDIDATES = 5000
MAX_WORKBOOK_EXPANDED_BYTES = 20 * 1024 * 1024
MAX_EXAM_QUESTIONS = 500
ACCESS_CODE_PREFIX = "EG-"

_HEADER_ALIASES = {
    "candidate_name": {"candidate_name", "full_name", "name"},
    "registration_number": {"registration_number", "registration_no", "reg_no"},
}
_XML_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_XML_REL_DOC = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_XML_REL_PACKAGE = "{http://schemas.openxmlformats.org/package/2006/relationships}"


class CloudExamServiceError(ValueError):
    """A cloud exam request or workbook failed validation."""


@dataclass(frozen=True)
class RosterCandidate:
    candidate_name: str
    registration_number: str
    registration_key: str


@dataclass(frozen=True)
class PreparedRoster:
    count: int
    records: tuple[dict[str, str], ...] = field(repr=False)
    credentials_csv: bytes = field(repr=False)


def _normalize_registration(registration_number: str) -> str:
    return re.sub(r"\s+", "", registration_number.strip().casefold())


def _normalize_header(header: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", header.strip().casefold()).strip("_")


def _column_index(cell_reference: str) -> int:
    match = re.match(r"([A-Z]+)", cell_reference.upper())
    if not match:
        raise CloudExamServiceError("The XLSX sheet contains an invalid cell reference.")
    value = 0
    for character in match.group(1):
        value = value * 26 + ord(character) - ord("A") + 1
    return value - 1


def _read_xlsx_rows(content: bytes) -> list[list[str]]:
    try:
        workbook = zipfile.ZipFile(io.BytesIO(content))
    except (zipfile.BadZipFile, OSError) as error:
        raise CloudExamServiceError("The XLSX file is damaged or invalid.") from error

    with workbook:
        if len(workbook.infolist()) > 128:
            raise CloudExamServiceError("The XLSX workbook contains too many internal files.")
        expanded_size = sum(info.file_size for info in workbook.infolist())
        if expanded_size > MAX_WORKBOOK_EXPANDED_BYTES:
            raise CloudExamServiceError("The expanded XLSX workbook is too large.")

        try:
            workbook_root = ElementTree.fromstring(workbook.read("xl/workbook.xml"))
            relations_root = ElementTree.fromstring(workbook.read("xl/_rels/workbook.xml.rels"))
        except (KeyError, ElementTree.ParseError) as error:
            raise CloudExamServiceError("The XLSX workbook structure is invalid.") from error

        first_sheet = workbook_root.find(f".//{_XML_MAIN}sheet")
        if first_sheet is None:
            raise CloudExamServiceError("The XLSX workbook does not contain a worksheet.")

        relationship_id = first_sheet.get(f"{_XML_REL_DOC}id")
        relationship = next(
            (
                item
                for item in relations_root.findall(f"{_XML_REL_PACKAGE}Relationship")
                if item.get("Id") == relationship_id
            ),
            None,
        )
        if relationship is None:
            raise CloudExamServiceError("The first XLSX worksheet could not be located.")

        target = relationship.get("Target", "")
        sheet_path = (
            target.lstrip("/")
            if target.startswith("/")
            else posixpath.normpath(posixpath.join("xl", target))
        )
        if not sheet_path.startswith("xl/") or ".." in sheet_path.split("/"):
            raise CloudExamServiceError("The XLSX worksheet path is invalid.")

        shared_strings: list[str] = []
        if "xl/sharedStrings.xml" in workbook.namelist():
            try:
                strings_root = ElementTree.fromstring(workbook.read("xl/sharedStrings.xml"))
            except ElementTree.ParseError as error:
                raise CloudExamServiceError("The XLSX shared-string table is invalid.") from error
            for item in strings_root.findall(f"{_XML_MAIN}si"):
                shared_strings.append(
                    "".join(part.text or "" for part in item.iter(f"{_XML_MAIN}t"))
                )

        try:
            sheet_root = ElementTree.fromstring(workbook.read(sheet_path))
        except (KeyError, ElementTree.ParseError) as error:
            raise CloudExamServiceError("The first XLSX worksheet is invalid.") from error

        rows: list[list[str]] = []
        for row_element in sheet_root.findall(f".//{_XML_MAIN}sheetData/{_XML_MAIN}row"):
            cells: dict[int, str] = {}
            for cell in row_element.findall(_XML_MAIN + "c"):
                reference = cell.get("r", "")
                value_element = cell.find(_XML_MAIN + "v")
                cell_type = cell.get("t", "")
                if cell_type == "inlineStr":
                    value = "".join(
                        part.text or "" for part in cell.iter(_XML_MAIN + "t")
                    )
                elif value_element is None:
                    value = ""
                elif cell_type == "s":
                    try:
                        shared_string_index = int(value_element.text or "")
                        if not 0 <= shared_string_index < len(shared_strings):
                            raise IndexError
                        value = shared_strings[shared_string_index]
                    except (ValueError, IndexError) as error:
                        raise CloudExamServiceError(
                            "The XLSX file contains an invalid shared-string reference."
                        ) from error
                else:
                    value = value_element.text or ""
                cells[_column_index(reference)] = value

            if cells:
                rows.append([cells.get(index, "") for index in range(max(cells) + 1)])
            else:
                rows.append([])
            if len(rows) > MAX_ROSTER_CANDIDATES + 1:
                raise CloudExamServiceError(
                    f"A roster can contain at most {MAX_ROSTER_CANDIDATES} candidates."
                )
        return rows


def _read_csv_rows(content: bytes) -> list[list[str]]:
    try:
        text = content.decode("utf-8-sig")
        rows: list[list[str]] = []
        for row in csv.reader(io.StringIO(text, newline="")):
            rows.append(row)
            if len(rows) > MAX_ROSTER_CANDIDATES + 1:
                raise CloudExamServiceError(
                    f"A roster can contain at most {MAX_ROSTER_CANDIDATES} candidates."
                )
        return rows
    except (UnicodeDecodeError, csv.Error) as error:
        raise CloudExamServiceError(
            "The CSV must be valid UTF-8 with a readable header."
        ) from error


def _extract_roster_rows(filename: str, content: bytes) -> list[list[str]]:
    if not isinstance(content, bytes) or not content:
        raise CloudExamServiceError("Choose a non-empty CSV or XLSX roster file.")
    if len(content) > MAX_ROSTER_UPLOAD_BYTES:
        raise CloudExamServiceError("The roster upload must be 5 MB or smaller.")

    extension = filename.rsplit(".", 1)[-1].casefold() if "." in filename else ""
    if extension == "csv":
        rows = _read_csv_rows(content)
    elif extension == "xlsx":
        rows = _read_xlsx_rows(content)
    else:
        raise CloudExamServiceError(
            "Use a UTF-8 .csv or .xlsx file; legacy .xls is not supported."
        )

    if not rows:
        raise CloudExamServiceError("The roster file is empty.")
    return rows


def parse_roster_upload(filename: str, content: bytes) -> tuple[RosterCandidate, ...]:
    rows = _extract_roster_rows(filename, content)
    header_index = next(
        (
            index
            for index, row in enumerate(rows)
            if any(cell.strip() for cell in row)
        ),
        None,
    )
    if header_index is None:
        raise CloudExamServiceError("The roster file does not contain a header row.")

    headers = [_normalize_header(cell) for cell in rows[header_index]]
    selected_columns: dict[str, int] = {}
    for field_name, aliases in _HEADER_ALIASES.items():
        matches = [index for index, header in enumerate(headers) if header in aliases]
        if not matches:
            required = "candidate_name and registration_number"
            raise CloudExamServiceError(
                f"Include columns for {required}; accepted headers include full_name and reg_no."
            )
        selected_columns[field_name] = matches[0]

    candidates: list[RosterCandidate] = []
    seen_keys: set[str] = set()
    for line_number, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
        if not any(cell.strip() for cell in row):
            continue
        values = {
            field_name: row[column].strip() if column < len(row) else ""
            for field_name, column in selected_columns.items()
        }
        name = values["candidate_name"]
        registration_number = values["registration_number"]
        if not name or not registration_number:
            raise CloudExamServiceError(
                f"Roster row {line_number} needs both a candidate name and registration number."
            )
        if len(name) > 120 or len(registration_number) > 128:
            raise CloudExamServiceError(
                f"Roster row {line_number} exceeds the supported name or registration length."
            )

        normalized_registration = _normalize_registration(registration_number)
        registration_key = hashlib.sha256(
            normalized_registration.encode("utf-8")
        ).hexdigest()
        if registration_key in seen_keys:
            raise CloudExamServiceError(
                f"Registration number is duplicated in roster row {line_number}."
            )
        seen_keys.add(registration_key)
        candidates.append(
            RosterCandidate(name, registration_number, registration_key)
        )
        if len(candidates) > MAX_ROSTER_CANDIDATES:
            raise CloudExamServiceError(
                f"A roster can contain at most {MAX_ROSTER_CANDIDATES} candidates."
            )

    if not candidates:
        raise CloudExamServiceError("The roster file has no candidate rows.")
    return tuple(candidates)


def _escape_spreadsheet_cell(value: str) -> str:
    if value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def prepare_roster_upload(filename: str, content: bytes) -> PreparedRoster:
    """Prepare salted invitation verifiers and one-time downloadable credentials."""
    candidates = parse_roster_upload(filename, content)
    records: list[dict[str, str]] = []
    csv_buffer = io.StringIO(newline="")
    writer = csv.writer(csv_buffer)
    writer.writerow(("candidate_name", "registration_number", "access_code"))

    for candidate in candidates:
        access_code = ACCESS_CODE_PREFIX + secrets.token_urlsafe(24)
        salt = secrets.token_bytes(16)
        verifier = hashlib.sha256(salt + access_code.encode("utf-8")).hexdigest()
        records.append(
            {
                "candidate_name": candidate.candidate_name,
                "registration_number": candidate.registration_number,
                "registration_key": candidate.registration_key,
                "access_code_salt": salt.hex(),
                "access_code_hash": verifier,
            }
        )
        writer.writerow(
            (
                _escape_spreadsheet_cell(candidate.candidate_name),
                _escape_spreadsheet_cell(candidate.registration_number),
                access_code,
            )
        )

    return PreparedRoster(
        count=len(records),
        records=tuple(records),
        credentials_csv=csv_buffer.getvalue().encode("utf-8-sig"),
    )


def verify_access_code(access_code: str, salt_hex: str, expected_hash: str) -> bool:
    if not isinstance(access_code, str) or not access_code.strip():
        return False
    if not re.fullmatch(r"[0-9a-f]{32}", salt_hex or ""):
        return False
    if not re.fullmatch(r"[0-9a-f]{64}", expected_hash or ""):
        return False
    actual_hash = hashlib.sha256(
        bytes.fromhex(salt_hex) + access_code.strip().encode("utf-8")
    ).hexdigest()
    return hmac.compare_digest(actual_hash, expected_hash)


def prepare_question_snapshot(
    questions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(questions, list) or not 1 <= len(questions) <= MAX_EXAM_QUESTIONS:
        raise CloudExamServiceError(
            f"An exam must contain between 1 and {MAX_EXAM_QUESTIONS} questions."
        )

    snapshot: list[dict[str, Any]] = []
    for index, question in enumerate(questions):
        if not isinstance(question, dict):
            raise CloudExamServiceError(f"Question {index + 1} is invalid.")
        text = question.get("question")
        options = question.get("options")
        correct_index = question.get("correct_index", question.get("correct"))
        if (
            not isinstance(text, str)
            or not text.strip()
            or len(text) > 20000
            or not isinstance(options, list)
            or len(options) != 4
            or any(
                not isinstance(option, str)
                or not option.strip()
                or len(option) > 2000
                for option in options
            )
            or isinstance(correct_index, bool)
            or not isinstance(correct_index, int)
            or not 0 <= correct_index < 4
        ):
            raise CloudExamServiceError(f"Question {index + 1} is invalid.")
        snapshot.append(
            {
                "id": str(question.get("id", index + 1)),
                "question": text.strip(),
                "options": [option.strip() for option in options],
                "correct_index": correct_index,
            }
        )
    return snapshot


def public_questions(question_snapshot: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": question["id"],
            "question": question["question"],
            "options": list(question["options"]),
        }
        for question in question_snapshot
    ]


def candidate_attempt_payload(attempt: dict[str, Any]) -> dict[str, Any]:
    """Return attempt state without correct answers, scores, or candidate secrets."""
    questions = prepare_question_snapshot(attempt.get("question_snapshot"))
    answers = attempt.get("answers")
    if not isinstance(answers, list) or len(answers) != len(questions):
        raise CloudExamServiceError(
            "The saved cloud exam attempt has invalid answer progress."
        )
    return {
        "attempt_id": str(attempt["id"]),
        "attempt_number": int(attempt["attempt_number"]),
        "status": str(attempt["status"]),
        "questions": public_questions(questions),
        "answers": answers,
        "started_at": attempt["started_at"],
        "deadline_at": attempt["deadline_at"],
        "last_saved_at": attempt["last_saved_at"],
    }


def _require_aware_timestamp(value: str | datetime, label: str) -> str:
    try:
        parsed = (
            datetime.fromisoformat(value.replace("Z", "+00:00"))
            if isinstance(value, str)
            else value
        )
    except (TypeError, ValueError) as error:
        raise CloudExamServiceError(
            f"{label} must be a valid ISO timestamp with a timezone."
        ) from error
    if not isinstance(parsed, datetime) or parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CloudExamServiceError(f"{label} must include a timezone.")
    return parsed.astimezone(timezone.utc).isoformat()


def _response_data(response: Any) -> list[dict[str, Any]]:
    data = getattr(response, "data", None)
    if data is None:
        raise CloudExamServiceError("Supabase returned no data for the cloud exam operation.")
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return data
    if type(data) is int:
        return [{"count": data}]
    return []


class SupabaseCloudExamStore:
    """Server-side repository; give it only a Supabase service-role client."""

    def __init__(self, client: Any):
        if client is None:
            raise CloudExamServiceError(
                "Supabase is not configured. Set the server-side Supabase URL and secret key."
            )
        self._client = client

    @classmethod
    def from_env(cls) -> "SupabaseCloudExamStore":
        url = os.getenv("SUPABASE_URL", "").strip()
        secret_key = os.getenv("SUPABASE_SECRET_KEY", "").strip()
        if not url.startswith("https://") or not secret_key:
            raise CloudExamServiceError(
                "Cloud exams require SUPABASE_URL and a server-side Supabase service-role key "
                "in SUPABASE_SECRET_KEY."
            )
        try:
            from supabase import create_client
        except ImportError as error:
            raise CloudExamServiceError(
                "The Supabase Python client is not installed in this environment."
            ) from error
        return cls(create_client(url, secret_key))

    def create_exam(
        self,
        *,
        title: str,
        opens_at: str | datetime,
        closes_at: str | datetime,
        duration_minutes: int,
        attempt_limit: int,
        pass_marks: int,
        randomize_questions: bool,
        questions: list[dict[str, Any]],
        created_by: str,
    ) -> dict[str, Any]:
        title = title.strip() if isinstance(title, str) else ""
        created_by = created_by.strip() if isinstance(created_by, str) else ""
        if not title or len(title) > 160 or not created_by or len(created_by) > 120:
            raise CloudExamServiceError("Enter a valid exam title and administrator identity.")
        for label, value, minimum, maximum in (
            ("Duration", duration_minutes, 1, 480),
            ("Attempt limit", attempt_limit, 1, 20),
            ("Pass marks", pass_marks, 0, 100),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not minimum <= value <= maximum
            ):
                raise CloudExamServiceError(f"{label} is outside the supported range.")
        if not isinstance(randomize_questions, bool):
            raise CloudExamServiceError("Question randomization must be enabled or disabled.")

        open_time = _require_aware_timestamp(opens_at, "Exam start time")
        close_time = _require_aware_timestamp(closes_at, "Exam end time")
        if datetime.fromisoformat(open_time) >= datetime.fromisoformat(close_time):
            raise CloudExamServiceError("The exam end time must be after its start time.")

        payload = {
            "title": title,
            "status": "draft",
            "opens_at": open_time,
            "closes_at": close_time,
            "duration_minutes": duration_minutes,
            "attempt_limit": attempt_limit,
            "pass_marks": pass_marks,
            "randomize_questions": randomize_questions,
            "question_snapshot": prepare_question_snapshot(questions),
            "created_by": created_by,
        }
        response = self._client.table("cloud_exams").insert(payload).execute()
        records = _response_data(response)
        if len(records) != 1:
            raise CloudExamServiceError("Supabase did not confirm creation of the cloud exam.")
        return records[0]

    def import_roster(
        self,
        *,
        exam_id: str,
        filename: str,
        content: bytes,
        actor: str,
    ) -> PreparedRoster:
        prepared = prepare_roster_upload(filename, content)
        response = self._client.rpc(
            "import_cloud_exam_roster",
            {
                "p_exam_id": exam_id,
                "p_candidates": list(prepared.records),
                "p_actor": actor,
            },
        ).execute()
        imported = getattr(response, "data", None)
        if imported != prepared.count:
            raise CloudExamServiceError(
                "Supabase did not confirm the complete roster import; no credentials were returned."
            )
        return prepared

    def allow_candidate_login(
        self,
        *,
        exam_id: str,
        registration_number: str,
        source_address: str,
        secret_key: str,
    ) -> bool:
        normalized = _normalize_registration(registration_number)
        rate_key = hmac.new(
            secret_key.encode("utf-8"),
            f"{exam_id}\0{normalized}\0{source_address}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        response = self._client.rpc(
            "consume_cloud_exam_login_attempt",
            {
                "p_key_hash": rate_key,
                "p_max_attempts": 8,
                "p_window_seconds": 600,
            },
        ).execute()
        return getattr(response, "data", None) is True

    def authenticate_candidate(
        self,
        *,
        exam_id: str,
        registration_number: str,
        access_code: str,
    ) -> dict[str, str] | None:
        if not isinstance(registration_number, str) or not registration_number.strip():
            return None
        registration_key = hashlib.sha256(
            _normalize_registration(registration_number).encode("utf-8")
        ).hexdigest()
        response = (
            self._client.table("cloud_exam_candidates")
            .select(
                "id,candidate_name,registration_number,access_code_salt,"
                "access_code_hash,is_active"
            )
            .eq("exam_id", exam_id)
            .eq("registration_key", registration_key)
            .eq("is_active", True)
            .limit(1)
            .execute()
        )
        records = _response_data(response)
        if not records:
            return None
        record = records[0]
        if not verify_access_code(
            access_code,
            record.get("access_code_salt", ""),
            record.get("access_code_hash", ""),
        ):
            return None
        return {
            "id": str(record["id"]),
            "candidate_name": str(record["candidate_name"]),
            "registration_number": str(record["registration_number"]),
        }

    def set_exam_status(self, exam_id: str, status: str, actor: str) -> dict[str, Any]:
        if status not in {"open", "closed"}:
            raise CloudExamServiceError("The cloud exam status must be open or closed.")
        response = self._client.rpc(
            "set_cloud_exam_status",
            {"p_exam_id": exam_id, "p_status": status, "p_actor": actor},
        ).execute()
        records = _response_data(response)
        if len(records) != 1:
            raise CloudExamServiceError("Supabase did not confirm the exam status change.")
        return records[0]

    def start_attempt(self, exam_id: str, candidate_id: str) -> dict[str, Any]:
        response = (
            self._client.table("cloud_exams")
            .select("status,opens_at,closes_at,randomize_questions,question_snapshot")
            .eq("id", exam_id)
            .limit(1)
            .execute()
        )
        exams = _response_data(response)
        if not exams:
            raise CloudExamServiceError("The cloud exam was not found.")
        exam = exams[0]
        snapshot = prepare_question_snapshot(exam["question_snapshot"])
        if exam.get("randomize_questions"):
            secrets.SystemRandom().shuffle(snapshot)
        result = self._client.rpc(
            "start_cloud_exam_attempt",
            {
                "p_exam_id": exam_id,
                "p_candidate_id": candidate_id,
                "p_question_snapshot": snapshot,
            },
        ).execute()
        attempts = _response_data(result)
        if len(attempts) != 1:
            raise CloudExamServiceError("Supabase did not return the candidate's exam attempt.")
        return attempts[0]

    def get_candidate_attempt(
        self,
        exam_id: str,
        candidate_id: str,
        attempt_id: str,
    ) -> dict[str, Any] | None:
        response = (
            self._client.table("cloud_exam_attempts")
            .select(
                "id,exam_id,candidate_id,attempt_number,status,question_snapshot,"
                "answers,started_at,deadline_at,last_saved_at"
            )
            .eq("id", attempt_id)
            .eq("exam_id", exam_id)
            .eq("candidate_id", candidate_id)
            .limit(1)
            .execute()
        )
        records = _response_data(response)
        return records[0] if records else None

    def save_answers(
        self,
        exam_id: str,
        candidate_id: str,
        attempt_id: str,
        answers: list[int | None],
    ) -> dict[str, Any]:
        if (
            not isinstance(answers, list)
            or not answers
            or len(answers) > MAX_EXAM_QUESTIONS
            or any(
                answer is not None
                and (
                    isinstance(answer, bool)
                    or not isinstance(answer, int)
                    or not 0 <= answer < 4
                )
                for answer in answers
            )
        ):
            raise CloudExamServiceError("Exam answer progress is invalid.")
        response = self._client.rpc(
            "save_cloud_exam_answers",
            {
                "p_exam_id": exam_id,
                "p_candidate_id": candidate_id,
                "p_attempt_id": attempt_id,
                "p_answers": answers,
            },
        ).execute()
        records = _response_data(response)
        if len(records) != 1:
            raise CloudExamServiceError("Supabase did not confirm saving the exam answers.")
        return records[0]

    def submit_attempt(
        self,
        exam_id: str,
        candidate_id: str,
        attempt_id: str,
        answers: list[int | None],
        reason: str = "manual",
    ) -> dict[str, Any]:
        response = self._client.rpc(
            "submit_cloud_exam_attempt",
            {
                "p_exam_id": exam_id,
                "p_candidate_id": candidate_id,
                "p_attempt_id": attempt_id,
                "p_answers": answers,
                "p_reason": reason,
            },
        ).execute()
        records = _response_data(response)
        if len(records) != 1:
            raise CloudExamServiceError("Supabase did not confirm exam submission.")
        return records[0]

    def publish_results(self, exam_id: str, actor: str) -> str:
        response = self._client.rpc(
            "publish_cloud_exam_results",
            {"p_exam_id": exam_id, "p_actor": actor},
        ).execute()
        value = getattr(response, "data", None)
        if not isinstance(value, str) or not value:
            raise CloudExamServiceError("Supabase did not confirm result publication.")
        return value

    def list_admin_exams(self) -> list[dict[str, Any]]:
        response = self._client.rpc(
            "list_cloud_exam_admin_summary",
            {"p_limit": 200},
        ).execute()
        return _response_data(response)

    def get_admin_exam_detail(self, exam_id: str) -> dict[str, Any] | None:
        response = self._client.rpc(
            "get_cloud_exam_admin_detail",
            {"p_exam_id": exam_id},
        ).execute()
        data = getattr(response, "data", None)
        return data if isinstance(data, dict) else None

    def export_exam_results(self, exam_id: str) -> list[dict[str, Any]]:
        response = self._client.rpc(
            "export_cloud_exam_results",
            {"p_exam_id": exam_id},
        ).execute()
        return _response_data(response)

    def get_public_exam(self, exam_id: str) -> dict[str, Any] | None:
        response = (
            self._client.table("cloud_exams")
            .select(
                "id,title,status,opens_at,closes_at,duration_minutes,"
                "attempt_limit,pass_marks,results_published_at"
            )
            .eq("id", exam_id)
            .limit(1)
            .execute()
        )
        records = _response_data(response)
        return records[0] if records else None

    def record_proctoring_event(
        self,
        exam_id: str,
        attempt_id: str,
        event_type: str,
        image_bytes: bytes,
    ) -> bool:
        response = self._client.rpc(
            "record_cloud_exam_proctoring_event",
            {
                "p_exam_id": exam_id,
                "p_attempt_id": attempt_id,
                "p_event_type": event_type,
            },
        ).execute()
        event_id = getattr(response, "data", None)
        if type(event_id) is not int:
            raise CloudExamServiceError("Supabase did not confirm the proctoring event.")

        object_path = f"{exam_id}/{attempt_id}/{event_id}-{secrets.token_hex(8)}.jpg"
        storage = self._client.storage.from_("cloud-exam-evidence")
        storage.upload(
            object_path,
            image_bytes,
            file_options={"content-type": "image/jpeg", "upsert": "false"},
        )
        update_response = (
            self._client.table("cloud_exam_proctoring_events")
            .update({"evidence_object_path": object_path})
            .eq("id", event_id)
            .eq("attempt_id", attempt_id)
            .select("id")
            .execute()
        )
        if getattr(update_response, "data", None) is None:
            raise CloudExamServiceError("Supabase did not confirm saving event evidence.")
        return True

    def get_published_candidate_result(
        self,
        exam_id: str,
        candidate_id: str,
    ) -> dict[str, Any] | None:
        exam_response = (
            self._client.table("cloud_exams")
            .select("id,title,results_published_at")
            .eq("id", exam_id)
            .limit(1)
            .execute()
        )
        exams = _response_data(exam_response)
        if not exams or not exams[0].get("results_published_at"):
            return None

        attempt_response = (
            self._client.table("cloud_exam_attempts")
            .select("attempt_number,status,score,total_questions,passed,submitted_at")
            .eq("exam_id", exam_id)
            .eq("candidate_id", candidate_id)
            .order("attempt_number", desc=True)
            .limit(1)
            .execute()
        )
        attempts = _response_data(attempt_response)
        if not attempts or attempts[0].get("status") != "submitted":
            return None
        return {
            "exam_title": exams[0]["title"],
            "results_published_at": exams[0]["results_published_at"],
            **attempts[0],
        }
