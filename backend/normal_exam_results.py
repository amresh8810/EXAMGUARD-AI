"""Durable result storage and publication for the normal candidate exam flow."""

import re
from datetime import datetime, timezone


TABLE_NAME = "normal_exam_submissions"


class NormalExamResultError(RuntimeError):
    """Raised when a normal exam result cannot be durably stored or published."""


def calculate_result(questions: list[dict], answers: list[int | None]) -> dict:
    if not isinstance(questions, list) or not questions:
        raise NormalExamResultError("The submitted exam has no question snapshot.")
    if not isinstance(answers, list) or len(answers) != len(questions):
        raise NormalExamResultError("The submitted answers do not match the exam.")

    score = 0
    for question, answer in zip(questions, answers):
        correct = question.get("correct")
        if (
            isinstance(correct, bool)
            or not isinstance(correct, int)
            or not 0 <= correct < 4
        ):
            raise NormalExamResultError(
                "This exam is missing its saved answer key and cannot be scored safely."
            )
        if answer is not None and (
            isinstance(answer, bool)
            or not isinstance(answer, int)
            or not 0 <= answer < 4
        ):
            raise NormalExamResultError("A submitted answer is invalid.")
        if answer == correct:
            score += 1

    total = len(questions)
    passing_percentage = 50
    return {
        "score": score,
        "total_questions": total,
        "score_percentage": round(score * 100 / total, 2),
        "passing_percentage": passing_percentage,
        "passed": score * 100 >= total * passing_percentage,
    }


def _client_or_raise(client):
    if client is None:
        raise NormalExamResultError(
            "Supabase is not configured; this exam cannot be submitted durably."
        )
    return client


def get_submission(client, exam_id: str, registration_key: str | None = None):
    client = _client_or_raise(client)
    try:
        query = client.table(TABLE_NAME).select("*").eq("exam_id", exam_id)
        if registration_key is not None:
            query = query.eq("registration_key", registration_key)
        response = query.limit(1).execute()
    except Exception as error:
        raise NormalExamResultError(
            "Supabase could not load the exam result."
        ) from error
    records = getattr(response, "data", None)
    if not isinstance(records, list):
        raise NormalExamResultError("Supabase returned an invalid exam result.")
    return records[0] if records else None


def save_submission(client, payload: dict):
    client = _client_or_raise(client)
    try:
        response = client.table(TABLE_NAME).insert(payload).select("*").execute()
    except Exception as insert_error:
        try:
            existing = get_submission(client, payload["exam_id"])
        except Exception as lookup_error:
            raise NormalExamResultError(
                "Supabase could not confirm whether the exam result was saved."
            ) from lookup_error
        if existing is None:
            raise NormalExamResultError(
                "The exam result could not be saved to Supabase."
            ) from insert_error
        if (
            any(
                existing.get(field) != payload.get(field)
                for field in (
                    "registration_key",
                    "candidate_name",
                    "reg_no",
                    "question_snapshot",
                    "answers",
                    "score",
                    "total_questions",
                    "passing_percentage",
                    "passed",
                )
            )
        ):
            raise NormalExamResultError(
                "The saved exam result does not match this submission."
            ) from insert_error
        return existing

    records = getattr(response, "data", None)
    if not isinstance(records, list) or len(records) != 1:
        raise NormalExamResultError("Supabase did not confirm saving the exam result.")
    return records[0]


def list_submissions(client, limit: int = 200) -> list[dict]:
    client = _client_or_raise(client)
    try:
        response = (
            client.table(TABLE_NAME)
            .select("*")
            .order("submitted_at", desc=True)
            .limit(limit)
            .execute()
        )
    except Exception as error:
        raise NormalExamResultError(
            "Supabase could not load submitted exam results."
        ) from error
    records = getattr(response, "data", None)
    if not isinstance(records, list):
        raise NormalExamResultError("Supabase returned an invalid exam result list.")
    return records


def get_latest_published_submission(client, registration_key: str) -> dict | None:
    client = _client_or_raise(client)
    if not isinstance(registration_key, str) or not re.fullmatch(
        r"[0-9a-f]{64}",
        registration_key,
    ):
        raise NormalExamResultError("The candidate registration key is invalid.")

    try:
        response = (
            client.table(TABLE_NAME)
            .select("*")
            .eq("registration_key", registration_key)
            .eq("result_status", "published")
            .order("submitted_at", desc=True)
            .limit(1)
            .execute()
        )
    except Exception as error:
        raise NormalExamResultError(
            "Supabase could not load published exam results for this candidate."
        ) from error
    records = getattr(response, "data", None)
    if not isinstance(records, list):
        raise NormalExamResultError(
            "Supabase returned an invalid published exam result."
        )
    return records[0] if records else None


def publish_submission(client, exam_id: str, published_by: str) -> dict:
    client = _client_or_raise(client)
    try:
        response = (
            client.table(TABLE_NAME)
            .update({
                "result_status": "published",
                "published_at": datetime.now(timezone.utc).isoformat(),
                "published_by": published_by,
            })
            .eq("exam_id", exam_id)
            .eq("result_status", "pending")
            .select("*")
            .execute()
        )
    except Exception as error:
        raise NormalExamResultError(
            "Supabase could not publish the exam result."
        ) from error
    records = getattr(response, "data", None)
    if isinstance(records, list) and len(records) == 1:
        return records[0]

    existing = get_submission(client, exam_id)
    if existing is None:
        raise NormalExamResultError("The submitted exam result was not found.")
    if existing.get("result_status") == "published":
        return existing
    raise NormalExamResultError("Supabase did not confirm publishing the result.")
