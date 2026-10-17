"""Bulk Mailer Core.

Orchestrates bulk/newsletter sending with rate limiting, macro processing,
PDF obfuscation, sender/subject rotation, and domain excludes.
"""
import os
import json
import time
import threading
import logging
from email.utils import formatdate
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional

from .db_manager import BulkDBManager
from .bulk_mime_builder import BulkMIMEBuilder
from .content_engine import BulkContentEngine
from .rate_limiter import RateLimiter
from .smtp_client import SMTPClient
from .pdf_macro import fill_pdf_macro

logger = logging.getLogger("bulk.core")


class BulkMailerCore:
    BATCH_SIZE = 200

    def __init__(self, db: BulkDBManager, mailing_id: int):
        self._db = db
        self._mailing_id = mailing_id
        self._shutdown = threading.Event()
        self._send_count = 0
        self._lock = threading.Lock()

    def stop(self):
        self._shutdown.set()

    def run(self):
        try:
            self._run_inner()
        except Exception as exc:
            logger.error("Mailing %d CRASHED: %s", self._mailing_id, exc, exc_info=True)
            try:
                self._db.update_mailing_status(self._mailing_id, "FAILED")
            except Exception:
                pass

    def _run_inner(self):
        mailing = self._load_mailing()
        if not mailing:
            logger.error("Mailing %d not found", self._mailing_id)
            return
        mailing = dict(mailing)

        domain_row = self._db._conn().execute(
            "SELECT * FROM domains WHERE id=?", (mailing["domain_id"],)).fetchone()
        smtp_row = self._db._conn().execute(
            "SELECT * FROM smtp_presets WHERE id=?", (mailing["smtp_preset_id"],)).fetchone()
        template_row = self._db._conn().execute(
            "SELECT * FROM message_templates WHERE id=?", (mailing["template_id"],)).fetchone()

        if not domain_row:
            logger.error("Mailing %d: domain_id=%d not found", self._mailing_id, mailing["domain_id"])
            return
        if not smtp_row:
            logger.error("Mailing %d: smtp_preset_id=%d not found", self._mailing_id, mailing["smtp_preset_id"])
            return
        if not template_row:
            logger.error("Mailing %d: template_id=%d not found", self._mailing_id, mailing["template_id"])
            return

        domain_row = dict(domain_row)
        smtp_row = dict(smtp_row)
        template_row = dict(template_row)

        macros = {r["name"]: json.loads(r["values_json"]) for r in self._db.get_macros()}
        engine = BulkContentEngine(macros)

        proxy_str = smtp_row.get("proxy", "")
        proxy_required = bool(proxy_str and smtp_row.get("proxy_required", False))
        smtp = SMTPClient(smtp_row["host"], smtp_row["port"],
                          smtp_row["username"], smtp_row["password"],
                          proxy=proxy_str, proxy_required=proxy_required,
                          send_mode=smtp_row.get("send_mode", "smtp") or "smtp",
                          region=smtp_row.get("ses_region", "") or "",
                          config_set=smtp_row.get("ses_config_set", "") or "")

        daily_limit = mailing["daily_limit"] or smtp_row.get("daily_limit", 0) or 0
        limiter = RateLimiter(daily_limit=daily_limit)

        domain = domain_row["domain"]
        from_email = domain_row["from_email"] or f"newsletter@{domain}"
        reply_to = domain_row.get("reply_to_email", "") or ""
        list_id_label = f"newsletter.{domain}"
        provider = smtp_row.get("provider_type", "generic")
        is_ses = provider.lower() in ("ses", "aws", "amazon")

        if is_ses:
            bounce_domain = ""
        else:
            bounce_sub = domain_row.get("bounce_subdomain") or domain_row.get("send_subdomain") or "mail"
            bounce_domain = f"{bounce_sub}.{domain}"

        html_files = json.loads(template_row["html_files_json"] or "[]")
        html_rotate = template_row["html_rotate_every"] or 0
        sender_list = json.loads(template_row["sender_rotate_json"] or "[]")
        sender_rotate = template_row["sender_rotate_every"] or 0
        subject_macro = template_row["subject_macro"] or ""
        pdf_path = template_row["pdf_path"] or ""
        pdf_macro_on = bool(template_row["pdf_macro_enabled"])

        pdf_bytes = None
        if pdf_path and os.path.isfile(pdf_path):
            with open(pdf_path, "rb") as f:
                pdf_bytes = f.read()

        html_contents = []
        for hf in html_files:
            if os.path.isfile(hf):
                with open(hf, "r", encoding="utf-8") as f:
                    html_contents.append(f.read())

        exclude_domains = json.loads(mailing["exclude_domains_json"] or "[]")
        list_id_db = mailing["list_id"]

        feedback_base = f"newsletter:{domain}:m{self._mailing_id}"
        test_email = mailing.get("test_email", "")
        test_interval = mailing.get("test_interval", 0) or 0

        self._db.update_mailing_status(self._mailing_id, "RUNNING")
        self._db.reset_in_progress(list_id_db)
        self._db.reset_daily_counts()

        c = self._db._conn()
        total_in_list = c.execute("SELECT COUNT(*) FROM leads WHERE list_id=?", (list_id_db,)).fetchone()[0]
        pending_count = c.execute("SELECT COUNT(*) FROM leads WHERE list_id=? AND state='PENDING'", (list_id_db,)).fetchone()[0]

        if pending_count == 0 and total_in_list > 0:
            logger.info("Mailing %d: 0 pending but %d leads exist — resetting all to PENDING",
                         self._mailing_id, total_in_list)
            c.execute("UPDATE leads SET state='PENDING' WHERE list_id=?", (list_id_db,))
            c.execute("UPDATE mailings SET sent=0, failed=0, excluded=0 WHERE id=?", (self._mailing_id,))
            c.commit()
            pending_count = total_in_list

        states = c.execute("SELECT state, COUNT(*) FROM leads WHERE list_id=? GROUP BY state", (list_id_db,)).fetchall()
        state_str = ", ".join(f"{s[0]}={s[1]}" for s in states) if states else "no leads"
        logger.info("Mailing %d starting: list_id=%d, total_in_list=%d, pending=%d, states=[%s], "
                     "smtp=%s, provider=%s, proxy=%s, excludes=%s",
                     self._mailing_id, list_id_db, total_in_list, pending_count, state_str,
                     smtp_row["host"], provider, proxy_str or "none", exclude_domains)

        sent = 0
        failed = 0
        excluded = 0

        threads_per_smtp = max(1, min(smtp_row.get("threads_per_smtp", 1) or 1, 50))
        logger.info("Mailing %d: threads_per_smtp=%d", self._mailing_id, threads_per_smtp)

        import queue
        mail_queue = queue.Queue()

        def _send_one(lead_id, email):
            """Build + send one mail. Returns ('sent'|'failed'|'transient')."""
            nonlocal sent, failed

            with self._lock:
                idx = self._send_count

            remaining = self._db.get_smtp_remaining(smtp_row["id"])
            if remaining <= 0:
                self._shutdown.set()
                return "transient"

            limiter.wait()

            cur_from_name = domain_row["from_name"] or "Newsletter"
            if sender_list:
                cur_from_name = engine.get_rotated_value(sender_list, idx, sender_rotate)

            cur_subject = subject_macro
            cur_subject = engine.process(cur_subject, email)

            html_template = ""
            if html_contents:
                html_idx = engine.get_rotated_value(
                    list(range(len(html_contents))), idx, html_rotate) if html_rotate else 0
                html_template = html_contents[int(html_idx)] if isinstance(html_idx, (int, float)) else html_contents[0]
            else:
                html_template = "<p>Hello {email_user}</p>"

            html_body = engine.process(html_template, email)
            plain_body = engine.html_to_plaintext(html_body)

            unsub_token = f"{lead_id}-{self._mailing_id}"
            unsub_domain = domain_row.get("unsub_domain") or f"unsub.{domain}"
            unsub_url = f"https://{unsub_domain}/u/{unsub_token}"
            unsub_mailto = f"unsub-{unsub_token}@{domain}"

            feedback_id = f"{feedback_base}:{domain.replace('.', '-')[:15]}"

            attachment = None
            if pdf_bytes:
                if pdf_macro_on:
                    mod_pdf = fill_pdf_macro(pdf_bytes)
                    attachment = (os.path.basename(pdf_path), mod_pdf)
                else:
                    attachment = (os.path.basename(pdf_path), pdf_bytes)

            try:
                effective_reply = reply_to if reply_to and reply_to != from_email else ""
                raw_msg, envelope_from, verp_tag = BulkMIMEBuilder.build_email(
                    from_name=engine.process(cur_from_name, email),
                    from_email=from_email,
                    reply_to_name="",
                    reply_to_email=effective_reply,
                    to_email=email,
                    subject=cur_subject,
                    html_body=html_body,
                    plain_body=plain_body,
                    list_id_token=list_id_label,
                    list_id_name=list_id_label.split(".")[0].title() if list_id_label else "",
                    unsubscribe_url=unsub_url,
                    unsubscribe_mailto=unsub_mailto,
                    feedback_id=feedback_id,
                    bounce_domain=bounce_domain,
                    recipient_id=str(lead_id),
                    attachment=attachment,
                    provider_type=provider,
                )

                date_line = f"Date: {formatdate(localtime=True, usegmt=False)}\r\n"
                raw_msg = date_line + raw_msg

                success, error, code = smtp.send(envelope_from, email, raw_msg)

                if success:
                    self._db.mark_sent(lead_id)
                    self._db.increment_smtp_sent(smtp_row["id"])
                    with self._lock:
                        sent += 1
                    logger.info("Mailing %d: sent %d to %s", self._mailing_id, sent, email)
                    if test_interval > 0 and test_email and sent % test_interval == 0:
                        try:
                            test_raw, test_env, _ = BulkMIMEBuilder.build_email(
                                from_name=engine.process(cur_from_name, test_email),
                                from_email=from_email,
                                reply_to_name="", reply_to_email=effective_reply,
                                to_email=test_email, subject=f"[TEST #{sent}] {cur_subject}",
                                html_body=html_body, plain_body=plain_body,
                                list_id_token=list_id_label,
                                unsubscribe_url=unsub_url, unsubscribe_mailto=unsub_mailto,
                                feedback_id=feedback_id,
                                bounce_domain=bounce_domain,
                                recipient_id="test",
                                provider_type=provider,
                            )
                            test_raw = f"Date: {formatdate(localtime=True, usegmt=False)}\r\n" + test_raw
                            smtp.send(test_env, test_email, test_raw)
                        except Exception as te:
                            logger.error("Test mail failed: %s", te)
                    return "sent"
                elif error.startswith("FATAL:"):
                    self._db.mark_failed(lead_id, error)
                    with self._lock:
                        failed += 1
                    logger.error("Mailing %d: FATAL for %s: %s", self._mailing_id, email, error)
                    return "failed"
                else:
                    self._db._conn().execute(
                        "UPDATE leads SET state='PENDING' WHERE id=?", (lead_id,))
                    self._db._conn().commit()
                    with self._lock:
                        failed += 1
                    logger.warning("Mailing %d: transient for %s: %s", self._mailing_id, email, error)
                    return "transient"

            except Exception as exc:
                self._db.mark_failed(lead_id, str(exc)[:500])
                with self._lock:
                    failed += 1
                logger.error("Send error: %s", exc)
                return "failed"

        def _worker_thread():
            while not self._shutdown.is_set():
                try:
                    lead_id, email = mail_queue.get(timeout=2)
                except queue.Empty:
                    break
                try:
                    _send_one(lead_id, email)
                except Exception:
                    pass
                with self._lock:
                    self._send_count += 1
                self._db.update_mailing_counts(self._mailing_id, sent, failed, excluded)

        while not self._shutdown.is_set():
            batch = self._db.fetch_pending(list_id_db, exclude_domains, self.BATCH_SIZE)
            logger.info("Mailing %d: fetched batch of %d leads", self._mailing_id, len(batch))
            if not batch:
                logger.info("Mailing %d: no more pending leads, finishing", self._mailing_id)
                break

            lead_ids = [r[0] for r in batch]
            self._db.mark_in_progress(lead_ids)

            for item in batch:
                mail_queue.put(item)

            threads = []
            for _ in range(threads_per_smtp):
                t = threading.Thread(target=_worker_thread, daemon=True)
                t.start()
                threads.append(t)
            for t in threads:
                t.join()

            self._db.update_mailing_counts(self._mailing_id, sent, failed, excluded)

        status = "FINISHED" if not self._shutdown.is_set() else "PAUSED"
        logger.info("Mailing %d %s: sent=%d, failed=%d, excluded=%d",
                     self._mailing_id, status, sent, failed, excluded)
        self._db.update_mailing_status(self._mailing_id, status)
        self._db.reset_in_progress(list_id_db)

        if mailing.get("brand_id"):
            self._db.mark_list_used(mailing["brand_id"], list_id_db, self._mailing_id)

    def _load_mailing(self):
        return self._db._conn().execute(
            "SELECT * FROM mailings WHERE id=?", (self._mailing_id,)).fetchone()
