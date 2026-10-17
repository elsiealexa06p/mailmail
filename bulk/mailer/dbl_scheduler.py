"""DBL Scheduler — asyncio-Task läuft im Bulk-Web-Prozess und prüft
periodisch alle enabled Domains gegen die konfigurierten Blocklists.
Schickt bei neuen Listings (oder nach `renotify_hours` erneut) Telegram.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime

from bulk.mailer.dbl_checker import check_domain, send_telegram, format_alert, DomainResult

logger = logging.getLogger("bulk.dbl.scheduler")

_task: asyncio.Task | None = None
_stop = asyncio.Event()
_last_result_snapshot: dict[str, dict] = {}  # domain -> last DomainResult.detail dict


def _load_settings(db):
    row = db._conn().execute("SELECT * FROM dbl_settings WHERE id=1").fetchone()
    return dict(row) if row else {}


def _load_watches(db, enabled_only: bool = True):
    sql = "SELECT * FROM dbl_watch"
    if enabled_only:
        sql += " WHERE enabled=1"
    sql += " ORDER BY domain"
    return [dict(r) for r in db._conn().execute(sql).fetchall()]


def _resolvers_from_setting(raw: str) -> list[str]:
    if not raw:
        return []
    return [r.strip() for r in raw.replace(";", ",").split(",") if r.strip()]


def check_one(db, watch_row: dict, settings: dict) -> DomainResult:
    """Führt einen einzelnen Check durch und persistiert das Ergebnis.
    Keine Telegram-Benachrichtigung — das macht der Caller.
    """
    r = check_domain(
        watch_row["domain"],
        check_dbl=bool(settings.get("check_dbl", 1)),
        check_surbl=bool(settings.get("check_surbl", 0)),
        check_uribl=bool(settings.get("check_uribl", 0)),
        dqs_key=(settings.get("dqs_key") or "").strip(),
        resolvers=_resolvers_from_setting(settings.get("resolvers") or ""),
    )
    now = datetime.utcnow().isoformat(" ", "seconds")
    status = r.status
    detail = r.detail_json()
    was_listed = watch_row.get("status") == "listed"
    is_listed = status == "listed"

    c = db._conn()
    if is_listed and not was_listed:
        c.execute("""UPDATE dbl_watch SET status=?, detail=?, last_checked_at=?,
                     listed_since=? WHERE id=?""",
                  (status, detail, now, now, watch_row["id"]))
    elif not is_listed and was_listed:
        c.execute("""UPDATE dbl_watch SET status=?, detail=?, last_checked_at=?,
                     listed_since=NULL WHERE id=?""",
                  (status, detail, now, watch_row["id"]))
    else:
        c.execute("""UPDATE dbl_watch SET status=?, detail=?, last_checked_at=?
                     WHERE id=?""",
                  (status, detail, now, watch_row["id"]))
    c.commit()
    return r


def _should_renotify(watch_row: dict, renotify_hours: int) -> bool:
    """Bei weiterhin gelisteter Domain: nach renotify_hours erneut benachrichtigen."""
    last = watch_row.get("last_notified_at")
    if not last:
        return True
    try:
        dt = datetime.fromisoformat(last)
    except Exception:
        return True
    hrs = (datetime.utcnow() - dt).total_seconds() / 3600.0
    return hrs >= max(1, int(renotify_hours or 24))


def run_cycle(db, settings: dict | None = None) -> dict:
    """Komplette Prüfrunde über alle enabled Domains + Telegram.
    Rückgabe: {"checked": n, "newly_listed": [...], "recovered": [...], "still_listed": [...]}
    """
    settings = settings or _load_settings(db)
    watches = _load_watches(db, enabled_only=True)
    newly_listed: list[DomainResult] = []
    recovered: list[DomainResult] = []
    still_listed_rows: list[dict] = []

    for w in watches:
        try:
            prev_status = w.get("status")
            r = check_one(db, w, settings)
            if r.status == "listed" and prev_status != "listed":
                newly_listed.append(r)
            elif r.status != "listed" and prev_status == "listed":
                recovered.append(r)
            elif r.status == "listed":
                still_listed_rows.append(w)
        except Exception as e:
            logger.exception("DBL check failed for %s: %s", w.get("domain"), e)

    # Telegram-Alerts
    token = (settings.get("telegram_bot_token") or "").strip()
    chat_id = (settings.get("telegram_chat_id") or "").strip()
    renotify_hours = int(settings.get("renotify_hours") or 24)

    renotify_results: list[DomainResult] = []
    if still_listed_rows and token and chat_id:
        # noch gelistet → ggf. erneut benachrichtigen
        for w in still_listed_rows:
            if _should_renotify(w, renotify_hours):
                # DomainResult aus detail-JSON rekonstruieren (nur fürs Formatieren)
                dr = DomainResult(domain=w["domain"], checked_at=time.time())
                try:
                    for name, d in json.loads(w.get("detail") or "{}").items():
                        from bulk.mailer.dbl_checker import ListResult
                        lr = ListResult(name=d.get("name", name), zone=d.get("zone", ""))
                        lr.listed = bool(d.get("listed"))
                        lr.codes = list(d.get("codes") or [])
                        lr.reasons = list(d.get("reasons") or [])
                        lr.error = d.get("error") or ""
                        dr.lists.append(lr)
                except Exception:
                    pass
                renotify_results.append(dr)

    if (newly_listed or recovered or renotify_results) and token and chat_id:
        text = format_alert(newly_listed + renotify_results, recovered)
        if text.strip():
            ok, err = send_telegram(token, chat_id, text)
            if ok:
                now = datetime.utcnow().isoformat(" ", "seconds")
                c = db._conn()
                for r in newly_listed + renotify_results:
                    c.execute("UPDATE dbl_watch SET last_notified_at=? WHERE domain=?",
                              (now, r.domain))
                c.commit()
            else:
                logger.warning("Telegram send failed: %s", err)

    summary = {
        "checked": len(watches),
        "newly_listed": [r.domain for r in newly_listed],
        "recovered": [r.domain for r in recovered],
        "still_listed": [w["domain"] for w in still_listed_rows],
        "notified": len(newly_listed) + len(renotify_results),
    }
    now = datetime.utcnow().isoformat(" ", "seconds")
    db._conn().execute("UPDATE dbl_settings SET last_run_at=?, last_run_summary=? WHERE id=1",
                       (now, json.dumps(summary, ensure_ascii=False)))
    db._conn().commit()
    return summary


async def _loop(db):
    """Hintergrund-Loop. Rechnet Intervall-basiert aber reagiert innerhalb von
    ~15s auf _stop und auf Änderungen am check_interval_minutes."""
    logger.info("DBL scheduler started")
    while not _stop.is_set():
        try:
            settings = _load_settings(db)
            interval_min = max(1, int(settings.get("check_interval_minutes") or 60))
            enabled = bool(settings.get("enabled", 1))
            if enabled:
                try:
                    summary = await asyncio.to_thread(run_cycle, db, settings)
                    logger.info("DBL cycle done: %s", summary)
                except Exception as e:
                    logger.exception("DBL cycle failed: %s", e)
            # schlaf in 15s-Häppchen, damit stop/intervall-change schnell greift
            slept = 0
            step = 15
            total = interval_min * 60
            while slept < total and not _stop.is_set():
                await asyncio.sleep(step)
                slept += step
        except Exception as e:
            logger.exception("DBL scheduler outer loop crashed: %s", e)
            await asyncio.sleep(30)
    logger.info("DBL scheduler stopped")


def start_scheduler(db) -> asyncio.Task | None:
    global _task
    if _task and not _task.done():
        return _task
    _stop.clear()
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
    _task = loop.create_task(_loop(db))
    return _task


def stop_scheduler():
    _stop.set()
