import sqlite3
import time

from backend.database import DB_NAME


REVIEW_STATUSES = {
    "unreviewed",
    "valid_alert",
    "false_positive",
    "needs_follow_up",
}
REVIEW_DECISIONS = REVIEW_STATUSES


def init_admin_review_db():
    conn = sqlite3.connect(DB_NAME)
    try:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS proctoring_evidence_reviews (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            exam_id TEXT,
            event_type TEXT NOT NULL,
            evidence_path TEXT NOT NULL UNIQUE,
            occurred_at REAL NOT NULL,
            review_status TEXT NOT NULL DEFAULT 'unreviewed',
            review_note TEXT NOT NULL DEFAULT '',
            reviewed_by TEXT,
            reviewed_at REAL,
            CHECK (review_status IN (
                'unreviewed',
                'valid_alert',
                'false_positive',
                'needs_follow_up'
            ))
        )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_evidence_reviews_exam "
            "ON proctoring_evidence_reviews(exam_id, occurred_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_evidence_reviews_status "
            "ON proctoring_evidence_reviews(review_status, occurred_at)"
        )
        conn.commit()
    finally:
        conn.close()


def record_proctoring_event(exam_id, event_type, evidence_path, occurred_at=None):
    conn = sqlite3.connect(DB_NAME)
    try:
        cursor = conn.execute("""
        INSERT OR IGNORE INTO proctoring_evidence_reviews(
            exam_id, event_type, evidence_path, occurred_at
        )
        VALUES (?, ?, ?, ?)
        """, (
            exam_id,
            event_type,
            evidence_path,
            occurred_at if occurred_at is not None else time.time(),
        ))
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def register_unassigned_evidence(evidence_path, event_type, occurred_at):
    return record_proctoring_event(
        None,
        event_type,
        evidence_path,
        occurred_at,
    )


def _escaped_contains(value):
    escaped = value.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return f"%{escaped}%"


def get_admin_reports(filters=None):
    filters = filters or {}
    conditions = []
    parameters = []
    candidate = filters.get("candidate", "").strip()
    exam_id = filters.get("exam_id", "").strip()
    date_from = filters.get("date_from", "").strip()
    date_to = filters.get("date_to", "").strip()

    if candidate:
        conditions.append("r.candidate_name COLLATE NOCASE LIKE ? ESCAPE '!'")
        parameters.append(_escaped_contains(candidate))
    if exam_id:
        conditions.append("es.exam_id LIKE ? ESCAPE '!'")
        parameters.append(_escaped_contains(exam_id))
    if date_from:
        conditions.append("date(r.timestamp) >= date(?)")
        parameters.append(date_from)
    if date_to:
        conditions.append("date(r.timestamp) <= date(?)")
        parameters.append(date_to)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(f"""
        SELECT
            r.id AS report_id,
            r.candidate_name,
            r.face_count,
            r.violations,
            r.evidence_count,
            r.timestamp,
            es.exam_id,
            (
                SELECT COUNT(*)
                FROM proctoring_evidence_reviews e
                WHERE e.exam_id = es.exam_id
            ) AS linked_event_count,
            (
                SELECT COUNT(*)
                FROM proctoring_evidence_reviews e
                WHERE e.exam_id = es.exam_id
                  AND e.review_status = 'unreviewed'
            ) AS unreviewed_count,
            (
                SELECT COUNT(*)
                FROM proctoring_evidence_reviews e
                WHERE e.exam_id = es.exam_id
                  AND e.review_status = 'false_positive'
            ) AS false_positive_count
        FROM exam_reports r
        LEFT JOIN exam_sessions es ON es.report_id = r.id
        {where_clause}
        ORDER BY r.id DESC
        """, parameters).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_admin_review_events(filters=None):
    filters = filters or {}
    conditions = []
    parameters = []
    candidate = filters.get("candidate", "").strip()
    exam_id = filters.get("exam_id", "").strip()
    status = filters.get("status", "all")

    if candidate:
        conditions.append(
            "COALESCE(s.candidate_name, '') COLLATE NOCASE LIKE ? ESCAPE '!'"
        )
        parameters.append(_escaped_contains(candidate))
    if exam_id:
        conditions.append("COALESCE(e.exam_id, '') LIKE ? ESCAPE '!'")
        parameters.append(_escaped_contains(exam_id))
    if status in REVIEW_STATUSES:
        conditions.append("e.review_status = ?")
        parameters.append(status)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(f"""
        SELECT
            e.event_id,
            e.exam_id,
            s.candidate_name,
            e.event_type,
            e.evidence_path,
            e.occurred_at,
            e.review_status,
            e.review_note,
            e.reviewed_by,
            e.reviewed_at
        FROM proctoring_evidence_reviews e
        LEFT JOIN exam_sessions s ON s.exam_id = e.exam_id
        {where_clause}
        ORDER BY e.occurred_at DESC, e.event_id DESC
        """, parameters).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def update_evidence_review(event_id, status, note, reviewed_by):
    if status not in REVIEW_DECISIONS:
        return False
    reviewed_at = time.time() if status != "unreviewed" else None
    reviewer = reviewed_by if reviewed_at is not None else None
    conn = sqlite3.connect(DB_NAME)
    try:
        cursor = conn.execute("""
        UPDATE proctoring_evidence_reviews
        SET review_status = ?, review_note = ?, reviewed_by = ?, reviewed_at = ?
        WHERE event_id = ?
        """, (
            status,
            note,
            reviewer,
            reviewed_at,
            event_id,
        ))
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()
