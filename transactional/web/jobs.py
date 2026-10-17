"""Zentrales Job-System für alle Bulk-Generatoren (S3, Cloudinary,
Redirects, google.share …). Jobs sind:
  – persistent im Server-Prozess (überleben Tab-Wechsel, Page-Reload)
  – abbrechbar (threading.Event, Worker prüft periodisch)
  – auflistbar pro User (Widget in base.html zeigt aktive Jobs überall)
  – strukturiert (title, log, done/total, status)

Worker-Pattern:
    job = job_manager.create("s3_auto_batch", uid, "Auto-Batch: 3×5×50",
                              total=750, page_url="/s3-logos")
    def worker():
        try:
            for i in range(total):
                if job.cancelled():
                    break
                # ... do work ...
                job.tick(ok=1)  # oder err=1
                job.log_line(f"#{i} done")
            job.finish("done")
        except Exception as e:
            job.finish("error", str(e))
    threading.Thread(target=worker, daemon=True).start()
    return job.id
"""
from __future__ import annotations
import itertools
import logging
import threading
import time
from typing import Optional

logger = logging.getLogger("trans.jobs")

_MAX_LOG_LINES = 50
_KEEP_FINISHED_SECONDS = 600  # 10 min


class Job:
    def __init__(self, jid: int, kind: str, user_id: int, title: str,
                 total: int, page_url: str = ""):
        self.id = jid
        self.kind = kind
        self.user_id = user_id
        self.title = title
        self.page_url = page_url
        self.total = int(total or 0)
        self.done = 0
        self.ok = 0
        self.errors = 0
        self.log: list = []
        self.status = "running"      # running | done | cancelled | error
        self.error_msg = ""
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()

    def cancel(self):
        self._cancel.set()

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def tick(self, ok: int = 0, err: int = 0, done_delta: int = 1):
        with self._lock:
            self.done += done_delta
            self.ok += ok
            self.errors += err

    def set_progress(self, done: int, ok: Optional[int] = None,
                      errors: Optional[int] = None):
        with self._lock:
            self.done = done
            if ok is not None:
                self.ok = ok
            if errors is not None:
                self.errors = errors

    def set_total(self, total: int):
        with self._lock:
            self.total = int(total or 0)

    def log_line(self, msg: str):
        with self._lock:
            self.log.append(str(msg)[:400])
            if len(self.log) > _MAX_LOG_LINES:
                self.log = self.log[-_MAX_LOG_LINES:]

    def finish(self, status: str = "done", error_msg: str = ""):
        with self._lock:
            if self.status == "running":
                # cancel wins über done
                if self._cancel.is_set():
                    self.status = "cancelled"
                else:
                    self.status = status
            self.error_msg = error_msg
            self.finished_at = time.time()

    def pct(self) -> int:
        if self.total <= 0:
            return 0
        return max(0, min(100, int(self.done * 100 / self.total)))

    def age(self) -> float:
        return time.time() - self.started_at

    def since_finished(self) -> float:
        if not self.finished_at:
            return 0.0
        return time.time() - self.finished_at


class JobManager:
    def __init__(self):
        self._jobs: dict = {}
        self._counter = itertools.count(1)
        self._lock = threading.Lock()

    def create(self, kind: str, user_id: int, title: str, total: int = 0,
                page_url: str = "") -> Job:
        with self._lock:
            self._gc_locked()
            jid = next(self._counter)
            job = Job(jid, kind, user_id, title, total, page_url)
            self._jobs[jid] = job
            return job

    def get(self, jid: int) -> Optional[Job]:
        return self._jobs.get(int(jid))

    def cancel(self, jid: int, user_id: int) -> bool:
        j = self.get(jid)
        if not j or j.user_id != user_id:
            return False
        j.cancel()
        j.log_line("cancel requested by user")
        return True

    def list_active(self, user_id: int) -> list:
        return sorted(
            [j for j in self._jobs.values()
             if j.user_id == user_id and j.status == "running"],
            key=lambda j: j.started_at, reverse=True)

    def list_recent(self, user_id: int, limit: int = 5) -> list:
        return sorted(
            [j for j in self._jobs.values()
             if j.user_id == user_id and j.status != "running"],
            key=lambda j: (j.finished_at or 0), reverse=True)[:limit]

    def _gc_locked(self):
        now = time.time()
        stale = [jid for jid, j in self._jobs.items()
                  if j.finished_at and (now - j.finished_at) > _KEEP_FINISHED_SECONDS]
        for jid in stale:
            self._jobs.pop(jid, None)


job_manager = JobManager()
