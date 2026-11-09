"""Run the app locally in SFace test mode against a temporary DB copy."""

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend import database


def copy_database_read_only(source_path: Path, target_path: Path) -> None:
    source_uri = f"{source_path.resolve().as_uri()}?mode=ro"
    source = sqlite3.connect(source_uri, uri=True)
    try:
        target = sqlite3.connect(target_path)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()


def main() -> None:
    original_database = Path(database.DB_NAME)
    if not original_database.is_file():
        raise FileNotFoundError(f"Local exam database not found: {original_database}")

    with tempfile.TemporaryDirectory(prefix="examguard-local-sface-") as temp_dir:
        test_database = Path(temp_dir) / "examguard.db"
        copy_database_read_only(original_database, test_database)
        database.DB_NAME = str(test_database)
        os.environ["EXAMGUARD_FACE_MATCHER"] = "sface"

        from frontend.app import app

        print("SFace local test mode. The original exam database will not be modified.")
        print("Open http://127.0.0.1:5000 and stop this server with Ctrl+C.")
        app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
