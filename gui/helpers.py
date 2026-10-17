import os
import sqlite3
import configparser
import tempfile
import webbrowser
import time

CONFIG_PATH = "config.ini"
LOG_FILE = "smtp_errors.log"
_events: list = []


def read_config() -> configparser.ConfigParser:
    cp = configparser.ConfigParser()
    if os.path.isfile(CONFIG_PATH):
        cp.read(CONFIG_PATH, encoding="utf-8")
    return cp


def save_config(cp: configparser.ConfigParser):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        cp.write(f)


def db_path() -> str:
    return read_config().get("database", "db_path", fallback="mailer.db")


def db_stats() -> dict:
    path = db_path()
    r = {"PENDING": 0, "SENT": 0, "FAILED": 0, "IN_PROGRESS": 0, "total": 0}
    if not os.path.isfile(path):
        return r
    try:
        conn = sqlite3.connect(path, timeout=5)
        conn.execute("PRAGMA busy_timeout=5000")
        for s, n in conn.execute("SELECT state, COUNT(*) FROM leads GROUP BY state"):
            r[s] = n
        r["total"] = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        conn.close()
    except Exception:
        pass
    return r


def log_tail(n: int = 80) -> str:
    if not os.path.isfile(LOG_FILE):
        return "(no log file)"
    try:
        with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
            return "".join(f.readlines()[-n:]) or "(empty)"
    except OSError:
        return "(error)"


def scan_files(folder: str, exts: tuple = (".txt",)) -> list:
    if not os.path.isdir(folder):
        return []
    return sorted(f for f in os.listdir(folder) if f.lower().endswith(exts))


def count_lines(filepath: str) -> int:
    if not os.path.isfile(filepath):
        return 0
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            return sum(1 for line in f if line.strip())
    except OSError:
        return 0


def preview_lines(filepath: str, n: int = 5) -> str:
    if not os.path.isfile(filepath):
        return "(file not found)"
    try:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            lines = [l.rstrip() for l in f if l.strip()][:n]
        return "\n".join(lines) + (f"\n... ({count_lines(filepath)} total)" if len(lines) >= n else "")
    except OSError:
        return "(error)"


def open_html_in_browser(html: str):
    with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
        f.write(html)
        webbrowser.open(f"file://{f.name}")


def open_raw_in_browser(raw: str):
    html = f"<html><body><pre style='font-family:Consolas,monospace;font-size:13px;white-space:pre-wrap;background:#1e1e1e;color:#d4d4d4;padding:20px'>{_esc(raw)}</pre></body></html>"
    open_html_in_browser(html)


def log_event(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    _events.append(f"{ts}  {msg}")
    if len(_events) > 200:
        _events.pop(0)


def get_events() -> list:
    return list(_events)


def _esc(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
