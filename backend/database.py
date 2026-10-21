import os
import json
import sqlite3
import time
from contextlib import contextmanager
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(BASE_DIR, ".env"))

DB_DIR = os.path.join(BASE_DIR, "database")
os.makedirs(DB_DIR, exist_ok=True)
DB_NAME = os.path.join(DB_DIR, "examguard.db")

# ------------------------------------
# Supabase Cloud Client Initialization
# ------------------------------------
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = (os.getenv("SUPABASE_SECRET_KEY") or os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_PUBLISHABLE_KEY") or "").strip()
supabase = None

if SUPABASE_URL and SUPABASE_KEY and SUPABASE_URL.startswith("http"):
    try:
        from supabase import create_client
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        print(" Connected to Supabase Cloud Database successfully!")
    except Exception as e:
        print(" Supabase connection error, using local database:", e)
        supabase = None


def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS exam_reports(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        candidate_name TEXT,
        face_count INTEGER,
        violations INTEGER,
        evidence_count INTEGER,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS questions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        question_text TEXT,
        option_a TEXT,
        option_b TEXT,
        option_c TEXT,
        option_d TEXT,
        correct_option INTEGER DEFAULT 0
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS exam_sessions(
        exam_id TEXT PRIMARY KEY,
        resume_token_hash TEXT NOT NULL UNIQUE,
        candidate_name TEXT NOT NULL,
        reg_no TEXT NOT NULL,
        questions_json TEXT NOT NULL,
        answers_json TEXT NOT NULL,
        reviewed_json TEXT NOT NULL,
        visited_json TEXT NOT NULL,
        current_question INTEGER NOT NULL DEFAULT 0,
        started_at REAL NOT NULL,
        deadline_at REAL NOT NULL,
        last_saved_at REAL NOT NULL,
        status TEXT NOT NULL DEFAULT 'in_progress',
        submitted_at REAL,
        face_count INTEGER,
        violations INTEGER,
        evidence_count INTEGER,
        report_id INTEGER
    )
    """)
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_exam_sessions_resume_token "
        "ON exam_sessions(resume_token_hash)"
    )
    retention_cutoff = time.time() - 30 * 24 * 60 * 60
    cursor.execute("""
    DELETE FROM exam_sessions
    WHERE (status = 'submitted' AND submitted_at < ?)
       OR (status = 'in_progress' AND last_saved_at < ?)
    """, (retention_cutoff, retention_cutoff))

    cursor.execute("SELECT COUNT(*) FROM questions")
    if cursor.fetchone()[0] == 0:
        default_questions = [
            ("Which protocol provides reliable transport in TCP/IP?", "TCP", "UDP", "ICMP", "ARP", 0),
            ("Which layer is responsible for routing?", "Transport", "Network", "Session", "Presentation", 1),
            ("What does DNS translate?", "IP to MAC", "Domain to IP", "Port to IP", "MAC to IP", 1),
            ("Which protocol is connectionless?", "TCP", "FTP", "UDP", "SMTP", 2),
            ("Which device operates at Layer 2?", "Router", "Switch", "Firewall", "Gateway", 1),
            ("What is HTTP used for?", "Web communication", "Email", "Routing", "Encryption", 0),
            ("Which protocol sends email?", "SMTP", "FTP", "ARP", "DNS", 0),
            ("What is the default port of HTTPS?", "21", "25", "80", "443", 3),
            ("Which algorithm finds shortest path?", "Prim", "Kruskal", "Dijkstra", "DFS", 2),
            ("What is the primary function of an Operating System?", "Resource Management", "Hardware Production", "Compile Code", "Browse Web", 0)
        ]
        cursor.executemany("""
        INSERT INTO questions (question_text, option_a, option_b, option_c, option_d, correct_option)
        VALUES (?, ?, ?, ?, ?, ?)
        """, default_questions)

    conn.commit()
    conn.close()


def _exam_session_from_row(row):
    if row is None:
        return None
    return {
        "exam_id": row["exam_id"],
        "resume_token_hash": row["resume_token_hash"],
        "candidate_name": row["candidate_name"],
        "reg_no": row["reg_no"],
        "questions": json.loads(row["questions_json"]),
        "answers": json.loads(row["answers_json"]),
        "reviewed": json.loads(row["reviewed_json"]),
        "visited": json.loads(row["visited_json"]),
        "current_question": row["current_question"],
        "started_at": row["started_at"],
        "deadline_at": row["deadline_at"],
        "last_saved_at": row["last_saved_at"],
        "status": row["status"],
        "submitted_at": row["submitted_at"],
        "face_count": row["face_count"],
        "violations": row["violations"],
        "evidence_count": row["evidence_count"],
        "report_id": row["report_id"],
    }


@contextmanager
def _database_connection():
    conn = sqlite3.connect(DB_NAME)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_exam_session(
    exam_id,
    resume_token_hash,
    candidate_name,
    reg_no,
    questions,
    duration_seconds
):
    now = time.time()
    question_count = len(questions)
    with _database_connection() as conn:
        conn.execute("""
        INSERT INTO exam_sessions(
            exam_id, resume_token_hash, candidate_name, reg_no, questions_json,
            answers_json, reviewed_json, visited_json, current_question,
            started_at, deadline_at, last_saved_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
        """, (
            exam_id,
            resume_token_hash,
            candidate_name,
            reg_no,
            json.dumps(questions),
            json.dumps([None] * question_count),
            json.dumps([False] * question_count),
            json.dumps([True] + [False] * (question_count - 1)),
            now,
            now + duration_seconds,
            now,
        ))


def get_exam_session(exam_id):
    with _database_connection() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM exam_sessions WHERE exam_id = ?",
            (exam_id,)
        ).fetchone()
    return _exam_session_from_row(row)


def get_exam_session_by_token_hash(resume_token_hash):
    with _database_connection() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM exam_sessions WHERE resume_token_hash = ?",
            (resume_token_hash,)
        ).fetchone()
    return _exam_session_from_row(row)


def rotate_exam_resume_token(exam_id, resume_token_hash):
    with _database_connection() as conn:
        cursor = conn.execute("""
        UPDATE exam_sessions
        SET resume_token_hash = ?
        WHERE exam_id = ? AND status = 'in_progress'
        """, (resume_token_hash, exam_id))
    return cursor.rowcount == 1


def save_exam_progress(
    exam_id,
    answers,
    reviewed,
    visited,
    current_question
):
    now = time.time()
    with _database_connection() as conn:
        cursor = conn.execute("""
        UPDATE exam_sessions
        SET answers_json = ?, reviewed_json = ?, visited_json = ?,
            current_question = ?, last_saved_at = ?
        WHERE exam_id = ? AND status = 'in_progress' AND deadline_at > ?
        """, (
            json.dumps(answers),
            json.dumps(reviewed),
            json.dumps(visited),
            current_question,
            now,
            exam_id,
            now,
        ))
    return cursor.rowcount == 1


def complete_exam_session(
    exam_id,
    answers,
    reviewed,
    visited,
    current_question,
    face_count,
    violations,
    evidence_count
):
    now = time.time()
    with _database_connection() as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM exam_sessions WHERE exam_id = ?",
            (exam_id,)
        ).fetchone()
        if row is None:
            return None
        if row["status"] == "submitted":
            result = _exam_session_from_row(row)
            result["newly_submitted"] = False
            return result

        conn.execute("""
        UPDATE exam_sessions
        SET answers_json = ?, reviewed_json = ?, visited_json = ?,
            current_question = ?, last_saved_at = ?, status = 'submitted',
            submitted_at = ?, face_count = ?, violations = ?,
            evidence_count = ?
        WHERE exam_id = ? AND status = 'in_progress'
        """, (
            json.dumps(answers),
            json.dumps(reviewed),
            json.dumps(visited),
            current_question,
            now,
            now,
            face_count,
            violations,
            evidence_count,
            exam_id,
        ))
        cursor = conn.execute("""
        INSERT INTO exam_reports(
            candidate_name, face_count, violations, evidence_count
        )
        VALUES (?, ?, ?, ?)
        """, (
            row["candidate_name"],
            face_count,
            violations,
            evidence_count,
        ))
        report_id = cursor.lastrowid
        conn.execute(
            "UPDATE exam_sessions SET report_id = ? WHERE exam_id = ?",
            (report_id, exam_id)
        )
        completed = conn.execute(
            "SELECT * FROM exam_sessions WHERE exam_id = ?",
            (exam_id,)
        ).fetchone()
    result = _exam_session_from_row(completed)
    result["newly_submitted"] = True
    return result


def save_report(
    candidate_name,
    face_count,
    violations,
    evidence_count
):
    # 1. Save to local SQLite
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO exam_reports(
        candidate_name,
        face_count,
        violations,
        evidence_count
    )
    VALUES (?, ?, ?, ?)
    """, (
        candidate_name,
        face_count,
        violations,
        evidence_count
    ))
    conn.commit()
    conn.close()

    # 2. Sync to Supabase Cloud if available
    if supabase is not None:
        try:
            supabase.table("exam_reports").insert({
                "candidate_name": candidate_name,
                "face_count": face_count,
                "violations": violations,
                "evidence_count": evidence_count
            }).execute()
            print(" Report successfully synced to Supabase Cloud!")
        except Exception as e:
            print(" Supabase report insert failed:", e)


def get_all_reports():
    # If Supabase is connected, try to fetch cloud reports first
    if supabase is not None:
        try:
            res = supabase.table("exam_reports").select("*").order("id", desc=True).execute()
            if res.data:
                reports = []
                for r in res.data:
                    reports.append([
                        r.get("id"),
                        r.get("candidate_name"),
                        r.get("face_count", 0),
                        r.get("violations", 0),
                        r.get("evidence_count", 0),
                        r.get("timestamp") or r.get("created_at") or ""
                    ])
                return reports
        except Exception as e:
            print(" Supabase fetch failed, falling back to SQLite:", e)

    # Local SQLite fallback
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
    SELECT
        id,
        candidate_name,
        face_count,
        violations,
        evidence_count,
        timestamp
    FROM exam_reports
    ORDER BY id DESC
    """)
    reports = cursor.fetchall()
    conn.close()
    return reports


def get_all_questions():
    # If Supabase is connected, try to fetch cloud questions first
    if supabase is not None:
        try:
            res = supabase.table("questions").select("*").order("id", desc=False).execute()
            if res.data:
                return [{
                    "id": r.get("id"),
                    "question": r.get("question_text"),
                    "options": [r.get("option_a"), r.get("option_b"), r.get("option_c"), r.get("option_d")],
                    "correct": r.get("correct_option", 0)
                } for r in res.data]
        except Exception as e:
            print(" Supabase get_all_questions failed, falling back to SQLite:", e)

    # Local SQLite fallback
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
    SELECT id, question_text, option_a, option_b, option_c, option_d, correct_option
    FROM questions
    ORDER BY id ASC
    """)
    rows = cursor.fetchall()
    conn.close()
    questions = []
    for r in rows:
        questions.append({
            "id": r[0],
            "question": r[1],
            "options": [r[2], r[3], r[4], r[5]],
            "correct": r[6]
        })
    return questions


def add_question(question_text, option_a, option_b, option_c, option_d, correct_option=0):
    # 1. Local SQLite
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
    INSERT INTO questions (question_text, option_a, option_b, option_c, option_d, correct_option)
    VALUES (?, ?, ?, ?, ?, ?)
    """, (question_text, option_a, option_b, option_c, option_d, correct_option))
    conn.commit()
    new_id = cursor.lastrowid
    conn.close()

    # 2. Supabase Cloud Sync
    if supabase is not None:
        try:
            supabase.table("questions").insert({
                "question_text": question_text,
                "option_a": option_a,
                "option_b": option_b,
                "option_c": option_c,
                "option_d": option_d,
                "correct_option": correct_option
            }).execute()
            print(" Question synced to Supabase Cloud!")
        except Exception as e:
            print(" Supabase add_question failed:", e)

    return new_id


def delete_question(question_id):
    # 1. Local SQLite
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("DELETE FROM questions WHERE id = ?", (question_id,))
    conn.commit()
    conn.close()

    # 2. Supabase Cloud Sync
    if supabase is not None:
        try:
            supabase.table("questions").delete().eq("id", question_id).execute()
            print(" Question deleted from Supabase Cloud!")
        except Exception as e:
            print(" Supabase delete_question failed:", e)