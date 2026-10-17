import os
import sqlite3
import threading
from typing import List, Tuple

from .utils import EMAIL_RE


class DBManager:
    """Pragmatic lead queue.

    - No UNIQUE constraint on email: if leads.txt has 40x the same address,
      40 rows are inserted and 40 emails are sent.
    - Always-start: on every run, new lines in the leads file are appended
      to the DB. Already-imported lines are skipped via per-file line-count
      tracking. To start over completely, the user simply deletes the DB file.
    """

    STATE_PENDING = "PENDING"
    STATE_SENT = "SENT"
    STATE_FAILED = "FAILED"
    STATE_IN_PROGRESS = "IN_PROGRESS"

    def __init__(self, db_path: str = "mailer.db"):
        self._db_path = db_path
        self._local = threading.local()
        self._init_lock = threading.Lock()
        self._initialized = False
        self._ensure_schema()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self._db_path, timeout=30)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.execute("PRAGMA busy_timeout=10000")
        return self._local.conn

    def _ensure_schema(self) -> None:
        with self._init_lock:
            if self._initialized:
                return
            conn = self._get_conn()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'PENDING',
                    error_msg TEXT DEFAULT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_leads_state ON leads(state)
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS imported_files (
                    path TEXT PRIMARY KEY,
                    lines_imported INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS smtp_status (
                    host_key TEXT PRIMARY KEY,
                    fail_count INTEGER NOT NULL DEFAULT 0,
                    suspended_until REAL NOT NULL DEFAULT 0,
                    last_error TEXT DEFAULT NULL,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.commit()
            self._initialized = True

    def load_leads(self, leads_path: str) -> int:
        """Append new lines from leads_path (file or directory) to DB.

        Duplicates inside the file ARE preserved (no dedup).
        Lines already imported in previous runs are skipped automatically.
        Returns the number of newly inserted leads.
        """
        file_paths = self._resolve_paths(leads_path)
        if not file_paths:
            return 0

        conn = self._get_conn()
        total_inserted = 0

        for fpath in file_paths:
            abs_path = os.path.abspath(fpath)
            already = self._get_imported_count(conn, abs_path)
            current_total = self._count_file_lines(fpath)

            if current_total <= already:
                continue

            emails = self._parse_file(fpath, skip_lines=already)
            if not emails:
                self._set_imported_count(conn, abs_path, current_total)
                continue

            self._bulk_insert(conn, emails)
            self._set_imported_count(conn, abs_path, current_total)
            total_inserted += len(emails)

        return total_inserted

    @staticmethod
    def _resolve_paths(path: str) -> List[str]:
        if not path:
            return []
        if os.path.isfile(path):
            return [path]
        if os.path.isdir(path):
            found = []
            for root, _dirs, files in os.walk(path):
                for f in sorted(files):
                    if f.lower().endswith(".txt"):
                        found.append(os.path.join(root, f))
            return found
        return []

    @staticmethod
    def _count_file_lines(path: str) -> int:
        count = 0
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for _ in fh:
                    count += 1
        except OSError:
            pass
        return count

    @staticmethod
    def _parse_file(path: str, skip_lines: int = 0) -> List[str]:
        """Return list of (non-unique) email addresses from a file.

        Skips the first `skip_lines` lines (already-imported).
        Each email occurrence is preserved.
        """
        emails: List[str] = []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for i, line in enumerate(fh):
                    if i < skip_lines:
                        continue
                    for match in EMAIL_RE.findall(line):
                        emails.append(match.lower())
        except OSError:
            return []
        return emails

    @staticmethod
    def _get_imported_count(conn: sqlite3.Connection, abs_path: str) -> int:
        cur = conn.execute(
            "SELECT lines_imported FROM imported_files WHERE path = ?", (abs_path,)
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def _set_imported_count(conn: sqlite3.Connection, abs_path: str, count: int) -> None:
        conn.execute(
            "INSERT INTO imported_files (path, lines_imported) VALUES (?, ?) "
            "ON CONFLICT(path) DO UPDATE SET lines_imported = excluded.lines_imported",
            (abs_path, count),
        )
        conn.commit()

    def _bulk_insert(self, conn: sqlite3.Connection, emails: List[str]) -> None:
        batch_size = 500
        for i in range(0, len(emails), batch_size):
            batch = emails[i : i + batch_size]
            try:
                conn.executemany(
                    "INSERT INTO leads (email, state) VALUES (?, ?)",
                    [(e, self.STATE_PENDING) for e in batch],
                )
                conn.commit()
            except sqlite3.Error:
                conn.rollback()

    def fetch_pending_batch(self, batch_size: int = 100) -> List[Tuple[int, str]]:
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT id, email FROM leads WHERE state = ? ORDER BY id LIMIT ?",
            (self.STATE_PENDING, batch_size),
        )
        return cursor.fetchall()

    def mark_sent(self, lead_id: int) -> None:
        conn = self._get_conn()
        conn.execute(
            "UPDATE leads SET state = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (self.STATE_SENT, lead_id),
        )
        conn.commit()

    def mark_failed(self, lead_id: int, error_msg: str = "") -> None:
        conn = self._get_conn()
        conn.execute(
            "UPDATE leads SET state = ?, error_msg = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (self.STATE_FAILED, error_msg[:500], lead_id),
        )
        conn.commit()

    def mark_in_progress(self, lead_ids: List[int]) -> None:
        if not lead_ids:
            return
        conn = self._get_conn()
        placeholders = ",".join("?" for _ in lead_ids)
        conn.execute(
            f"UPDATE leads SET state = ?, updated_at = CURRENT_TIMESTAMP "
            f"WHERE id IN ({placeholders})",
            [self.STATE_IN_PROGRESS, *lead_ids],
        )
        conn.commit()

    def reset_in_progress(self) -> None:
        conn = self._get_conn()
        conn.execute(
            "UPDATE leads SET state = ? WHERE state = ?",
            (self.STATE_PENDING, self.STATE_IN_PROGRESS),
        )
        conn.commit()

    def count_by_state(self) -> dict:
        conn = self._get_conn()
        cursor = conn.execute("SELECT state, COUNT(*) FROM leads GROUP BY state")
        counts = {
            self.STATE_PENDING: 0,
            self.STATE_SENT: 0,
            self.STATE_FAILED: 0,
            self.STATE_IN_PROGRESS: 0,
        }
        for state, count in cursor.fetchall():
            counts[state] = count
        return counts

    def total_count(self) -> int:
        conn = self._get_conn()
        cursor = conn.execute("SELECT COUNT(*) FROM leads")
        return cursor.fetchone()[0]

    def retry_failed(self) -> int:
        conn = self._get_conn()
        cursor = conn.execute(
            "SELECT COUNT(*) FROM leads WHERE state = ?", (self.STATE_FAILED,)
        )
        count = cursor.fetchone()[0]
        if count > 0:
            conn.execute(
                "UPDATE leads SET state = ?, error_msg = NULL, "
                "updated_at = CURRENT_TIMESTAMP WHERE state = ?",
                (self.STATE_PENDING, self.STATE_FAILED),
            )
            conn.commit()
        return count

    def requeue_pending(self, lead_id: int) -> None:
        conn = self._get_conn()
        conn.execute(
            "UPDATE leads SET state = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (self.STATE_PENDING, lead_id),
        )
        conn.commit()

    def suspend_smtp(self, host_key: str, fail_count: int,
                     suspended_until: float, error: str = "") -> None:
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO smtp_status (host_key, fail_count, suspended_until, last_error, updated_at) "
            "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(host_key) DO UPDATE SET "
            "fail_count = excluded.fail_count, "
            "suspended_until = excluded.suspended_until, "
            "last_error = excluded.last_error, "
            "updated_at = CURRENT_TIMESTAMP",
            (host_key, fail_count, suspended_until, error[:500]),
        )
        conn.commit()

    def close(self) -> None:
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None
