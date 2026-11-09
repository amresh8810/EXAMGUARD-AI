"""Persistent open/closed state for the normal candidate exam."""

import sqlite3
from datetime import datetime, timezone

from backend.database import DB_NAME


TABLE_NAME = "normal_exam_availability"
ASSESSMENT_KEY = "normal_exam"


class NormalExamAvailabilityError(RuntimeError):
    """Raised when normal-exam availability cannot be read or changed."""


def _record_is_valid(record):
    return (
        isinstance(record, dict)
        and record.get("assessment_key") == ASSESSMENT_KEY
        and isinstance(record.get("is_open"), bool)
    )


def _ensure_sqlite_table(conn):
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
            assessment_key TEXT PRIMARY KEY
                CHECK (assessment_key = '{ASSESSMENT_KEY}'),
            is_open INTEGER NOT NULL CHECK (is_open IN (0, 1)),
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL
        )
        """
    )


def _sqlite_get_or_initialize(db_path):
    conn = None
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        _ensure_sqlite_table(conn)
        conn.execute(
            f"""
            INSERT OR IGNORE INTO {TABLE_NAME} (
                assessment_key, is_open, updated_at, updated_by
            ) VALUES (?, 1, ?, 'System')
            """,
            (ASSESSMENT_KEY, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        row = conn.execute(
            f"""
            SELECT assessment_key, is_open, updated_at, updated_by
            FROM {TABLE_NAME}
            WHERE assessment_key = ?
            """,
            (ASSESSMENT_KEY,),
        ).fetchone()
    except sqlite3.Error as error:
        raise NormalExamAvailabilityError(
            "The normal exam availability setting could not be loaded."
        ) from error
    finally:
        if conn is not None:
            conn.close()

    if row is None:
        raise NormalExamAvailabilityError(
            "The normal exam availability setting is missing."
        )
    return {
        "assessment_key": row[0],
        "is_open": bool(row[1]),
        "updated_at": row[2],
        "updated_by": row[3],
    }


def _sqlite_set_availability(is_open, updated_by, updated_at, db_path):
    conn = None
    try:
        conn = sqlite3.connect(db_path, timeout=5)
        _ensure_sqlite_table(conn)
        conn.execute(
            f"""
            INSERT INTO {TABLE_NAME} (
                assessment_key, is_open, updated_at, updated_by
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(assessment_key) DO UPDATE SET
                is_open = excluded.is_open,
                updated_at = excluded.updated_at,
                updated_by = excluded.updated_by
            """,
            (ASSESSMENT_KEY, int(is_open), updated_at, updated_by),
        )
        conn.commit()
    except sqlite3.Error as error:
        raise NormalExamAvailabilityError(
            "The normal exam availability setting could not be saved."
        ) from error
    finally:
        if conn is not None:
            conn.close()


def get_availability(client):
    if client is None:
        return _sqlite_get_or_initialize(DB_NAME)

    try:
        response = (
            client.table(TABLE_NAME)
            .select("assessment_key,is_open,updated_at,updated_by")
            .eq("assessment_key", ASSESSMENT_KEY)
            .limit(1)
            .execute()
        )
    except Exception as error:
        raise NormalExamAvailabilityError(
            "Supabase could not load normal exam availability. Confirm that "
            "database/normal_exam_availability.sql has been applied."
        ) from error

    records = getattr(response, "data", None)
    if not isinstance(records, list) or len(records) != 1:
        raise NormalExamAvailabilityError(
            "The normal exam availability setting is missing or invalid. "
            "Apply database/normal_exam_availability.sql in Supabase."
        )
    if not _record_is_valid(records[0]):
        raise NormalExamAvailabilityError(
            "Supabase returned an invalid normal exam availability setting."
        )
    return records[0]


def set_availability(client, is_open, updated_by):
    if not isinstance(is_open, bool):
        raise NormalExamAvailabilityError("Availability must be open or closed.")
    if (
        not isinstance(updated_by, str)
        or not updated_by.strip()
        or len(updated_by.strip()) > 120
    ):
        raise NormalExamAvailabilityError("The administrator identity is invalid.")

    updated_at = datetime.now(timezone.utc).isoformat()
    if client is None:
        _sqlite_set_availability(
            is_open,
            updated_by.strip(),
            updated_at,
            DB_NAME,
        )
        return get_availability(None)

    try:
        response = (
            client.table(TABLE_NAME)
            .update({
                "is_open": is_open,
                "updated_at": updated_at,
                "updated_by": updated_by.strip(),
            })
            .eq("assessment_key", ASSESSMENT_KEY)
            .select("assessment_key,is_open,updated_at,updated_by")
            .execute()
        )
    except Exception as error:
        raise NormalExamAvailabilityError(
            "Supabase could not update normal exam availability. Confirm that "
            "database/normal_exam_availability.sql has been applied."
        ) from error

    records = getattr(response, "data", None)
    if not isinstance(records, list) or len(records) != 1:
        raise NormalExamAvailabilityError(
            "Supabase did not confirm the normal exam availability update."
        )
    if not _record_is_valid(records[0]) or records[0]["is_open"] is not is_open:
        raise NormalExamAvailabilityError(
            "Supabase returned an invalid normal exam availability update."
        )
    return records[0]
