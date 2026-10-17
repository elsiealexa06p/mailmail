"""Warmup Engine — orchestrates warmup campaigns, schedules engagement actions."""
import random
import logging
import time
import threading
import datetime
from typing import List

from .db_manager import BulkDBManager
from .warmup_providers import get_curve_values
from .warmup_imap import IMAPWorker

logger = logging.getLogger("bulk.warmup")

ACTION_WEIGHTS = {
    "mark_read": 60,
    "rescue_spam": 10,
    "flag_important": 15,
    "click_links": 20,
    "send_reply": 3,
}


class WarmupEngine:
    """Runs warmup campaigns: sends to seeds, schedules and executes engagement."""

    def __init__(self, db: BulkDBManager):
        self._db = db
        self._shutdown = threading.Event()
        self._threads = {}

    def stop(self):
        self._shutdown.set()

    def run_campaign(self, campaign_id: int):
        """Main loop for a single warmup campaign."""
        camp = self._db.get_warmup_campaign(campaign_id)
        if not camp:
            logger.error("Warmup campaign %d not found", campaign_id)
            return
        camp = dict(camp)

        self._db.update_warmup_campaign(campaign_id, status="RUNNING")

        today = datetime.date.today().isoformat()
        if not camp["start_date"]:
            self._db.update_warmup_campaign(campaign_id, start_date=today)
            camp["start_date"] = today

        try:
            start = datetime.date.fromisoformat(camp["start_date"])
        except (ValueError, TypeError):
            start = datetime.date.today()
            self._db.update_warmup_campaign(campaign_id, start_date=start.isoformat())

        current_day = (datetime.date.today() - start).days + 1

        # Fixed-Daily-Target-Mode: wenn daily_fixed_target > 0, ignoriere Curve
        # und nimm den festen Wert (mit leichter Abweichung ±10%). Sonst Curve.
        fixed_target = int(camp.get("daily_fixed_target") or 0)
        if fixed_target > 0:
            variation = random.uniform(0.9, 1.1)
            daily_target = max(1, int(fixed_target * variation))
            seed_pct = 100
        else:
            daily_target, seed_pct = get_curve_values(camp.get("curve_type", "turbo"), current_day)
        self._db.update_warmup_campaign(campaign_id,
                                         current_day=current_day,
                                         daily_target=daily_target,
                                         seed_pct=seed_pct)

        logger.info("Warmup %d day %d: target=%d, seed_pct=%d%%",
                     campaign_id, current_day, daily_target, seed_pct)

        seeds = [dict(s) for s in self._db.get_seeds(active_only=True)]
        if not seeds:
            logger.warning("No active seeds for warmup %d", campaign_id)
            self._db.update_warmup_campaign(campaign_id, status="IDLE")
            return

        seed_count = max(1, int(len(seeds) * seed_pct / 100))
        num_sends = min(daily_target, seed_count)

        inactive_pct = 0.25
        num_inactive = int(len(seeds) * inactive_pct)
        active_seeds = seeds[num_inactive:]
        random.shuffle(active_seeds)
        selected = active_seeds[:num_sends]

        from_email = camp.get("from_email", "")
        domain = camp.get("sending_domain", "")
        from_filter = from_email.split("@")[0] if from_email else domain

        # Schedule engagement actions
        now = datetime.datetime.now()
        for seed in selected:
            roll = random.random()
            if roll > seed.get("open_rate", 0.7):
                continue

            delay_min = random.gauss(120, 60)
            delay_min = max(5, min(480, delay_min))
            action_time = now + datetime.timedelta(minutes=delay_min)

            actions = self._pick_actions(seed)
            for action in actions:
                jitter = random.gauss(0, 10)
                scheduled = action_time + datetime.timedelta(minutes=jitter)
                self._db.schedule_warmup_action(
                    campaign_id, seed["id"], action,
                    scheduled.strftime("%Y-%m-%d %H:%M:%S"))
                action_time += datetime.timedelta(minutes=random.uniform(2, 15))

        logger.info("Warmup %d: scheduled actions for %d/%d seeds",
                     campaign_id, len(selected), len(seeds))

        # Send warmup emails to selected seeds
        smtp_row = None
        if camp.get("smtp_preset_id"):
            smtp_row = self._db._conn().execute(
                "SELECT * FROM smtp_presets WHERE id=?",
                (camp["smtp_preset_id"],)).fetchone()
            if smtp_row:
                smtp_row = dict(smtp_row)

        template_row = None
        if camp.get("template_id"):
            template_row = self._db._conn().execute(
                "SELECT * FROM message_templates WHERE id=?",
                (camp["template_id"],)).fetchone()
            if template_row:
                template_row = dict(template_row)

        if smtp_row and from_email:
            sent = self._send_to_seeds(camp, smtp_row, template_row, selected, from_filter)
            self._db.update_warmup_campaign(campaign_id,
                                             sent_today=sent,
                                             last_send_date=today)
            logger.info("Warmup %d: sent %d warmup emails", campaign_id, sent)
        else:
            logger.warning("Warmup %d: no SMTP or from_email configured, skipping sends",
                            campaign_id)

        self._db.update_warmup_campaign(campaign_id, status="IDLE")

    def _pick_actions(self, seed: dict) -> List[str]:
        actions = []
        for action, weight in ACTION_WEIGHTS.items():
            if action == "send_reply" and random.random() > seed.get("reply_rate", 0.03):
                continue
            if action == "click_links" and random.random() > seed.get("click_rate", 0.2):
                continue
            if random.randint(1, 100) <= weight:
                actions.append(action)
        if not actions:
            actions = ["mark_read"]
        return actions

    def _send_to_seeds(self, camp: dict, smtp_row: dict, template_row,
                       seeds: list, from_filter: str) -> int:
        """Send warmup emails to seed accounts. Jede Mail kriegt frischen
        Content (Spintax pro Send), optional PDF-Anhang (uniquified), und
        einen unsichtbaren Byte-Uniquifier fürs HTML."""
        import json
        import os
        from .bulk_mime_builder import BulkMIMEBuilder
        from .content_engine import BulkContentEngine
        from .smtp_client import SMTPClient
        from .warmup_content import generate_local_email, make_unique_html
        from email.utils import formatdate

        macros = {r["name"]: json.loads(r["values_json"]) for r in self._db.get_macros()}
        engine = BulkContentEngine(macros)

        client = SMTPClient(smtp_row["host"], smtp_row["port"],
                            smtp_row["username"], smtp_row["password"],
                            proxy=smtp_row.get("proxy", ""),
                            send_mode=smtp_row.get("send_mode", "smtp") or "smtp",
                            region=smtp_row.get("ses_region", "") or "",
                            config_set=smtp_row.get("ses_config_set", "") or "")

        from_email = camp["from_email"]
        from_name = camp.get("from_name", "") or "Newsletter"
        domain = camp["sending_domain"]

        # LLM-Config nur checken, nicht mehr für BATCH benutzen — Content
        # kommt jetzt pro Send frisch aus dem lokalen Pool. LLM optional
        # via warmup_ai wenn Key konfiguriert.
        llm_cfg = self._db.get_llm_config()
        use_llm = bool(llm_cfg.get("api_key", "").strip())

        # PDF-Anhang-Config
        pdf_attach_pct = int(camp.get("pdf_attach_pct") or 0)
        pdf_available = pdf_attach_pct > 0

        sent = 0
        for seed in seeds:
            if self._shutdown.is_set():
                break
            try:
                to_email = seed["email"]

                # Content: pro Send frisch aus dem lokalen Pool
                content = generate_local_email(sender_hint=from_name)
                subject = content["subject"]
                html_body = content["html"]

                # Wenn ein spezifisches Template konfiguriert wurde,
                # nutz das statt Local-Pool (Override)
                if template_row:
                    html_files = json.loads(template_row.get("html_files_json", "[]") or "[]")
                    if html_files:
                        for hf in html_files:
                            if os.path.isfile(hf):
                                with open(hf, "r", encoding="utf-8") as f:
                                    html_body = f.read()
                                break
                    if template_row.get("subject_macro"):
                        subject = template_row["subject_macro"]

                # LLM-Overlay optional
                if use_llm and random.random() < 0.3:  # 30% LLM, 70% local
                    try:
                        from .warmup_ai import generate_warmup_email
                        ai = generate_warmup_email(
                            llm_cfg.get("api_url", ""), llm_cfg.get("api_key", ""),
                            llm_cfg.get("model", ""), domain, "de")
                        if ai.get("subject") and ai.get("html"):
                            subject = ai["subject"]
                            html_body = ai["html"]
                    except Exception:
                        pass

                # Macros + Byte-Uniquify pro Send
                processed_subject = engine.process(subject, to_email)
                processed_html = engine.process(html_body, to_email)
                processed_html = make_unique_html(processed_html)
                plain = engine.html_to_plaintext(processed_html)

                # PDF-Anhang würfeln
                pdf_bytes = None
                pdf_filename = None
                if pdf_available and random.random() * 100 < pdf_attach_pct:
                    pdf_bytes, pdf_filename = self._pick_uniquified_pdf()

                mime_kwargs = dict(
                    from_name=engine.process(from_name, to_email),
                    from_email=from_email,
                    reply_to_name="", reply_to_email="",
                    to_email=to_email, subject=processed_subject,
                    html_body=processed_html, plain_body=plain,
                    list_id_token=f"warmup.{domain}",
                    unsubscribe_url=f"https://unsub.{domain}/u/warmup",
                    unsubscribe_mailto=f"unsub-warmup@{domain}",
                    feedback_id=f"warmup:{domain}:w1:{domain.replace('.', '-')[:15]}",
                    provider_type=smtp_row.get("provider_type", "generic"),
                )
                if pdf_bytes and _mime_supports_pdf():
                    mime_kwargs["pdf_bytes"] = pdf_bytes
                    mime_kwargs["pdf_filename"] = pdf_filename
                raw, envelope, _ = BulkMIMEBuilder.build_email(**mime_kwargs)
                raw = f"Date: {formatdate(localtime=True, usegmt=False)}\r\n" + raw

                ok, err, code = client.send(envelope, to_email, raw)
                if ok:
                    sent += 1
                else:
                    logger.warning("Warmup send to %s failed: %s", to_email, err)

                time.sleep(random.uniform(2, 8))
            except Exception as e:
                logger.error("Warmup send error to %s: %s", seed["email"], e)

        return sent

    def _pick_uniquified_pdf(self):
        """Nimm ein random PDF aus dem Pool, uniquify Bytes (Metadaten +
        EOF-Padding, unsichtbar), gib (bytes, filename) zurück."""
        try:
            pdf_row = self._db.random_warmup_pdf()
            if not pdf_row:
                return None, None
            import os
            path = pdf_row.get("file_path", "")
            if not os.path.isfile(path):
                return None, None
            with open(path, "rb") as f:
                raw = f.read()
            # Uniquify: leichte Metadaten-Änderung via pikepdf falls verfügbar,
            # sonst nur EOF-Padding damit Hash-Änderung garantiert ist.
            try:
                from .pdf_variator import PDFVariator, LayerSet
                v = PDFVariator(raw, layers=LayerSet(
                    filename=False, metadata=True, structure=False,
                    image=False, byte_noise=True))
                _, uniq_bytes = v.make_variant(seed=random.randint(0, 2**31))
                fname = pdf_row.get("filename", "attachment.pdf")
                return uniq_bytes, fname
            except Exception:
                # Fallback ohne pikepdf: nur EOF-random-padding
                pad = b"\n%" + bytes(random.randint(0, 255) for _ in range(16)).hex().encode() + b"\n"
                return raw + pad, pdf_row.get("filename", "attachment.pdf")
        except Exception as e:
            logger.warning("PDF-Uniquify failed: %s", e)
            return None, None


    def execute_pending_actions(self):
        """Execute all pending engagement actions."""
        actions = [dict(a) for a in self._db.get_pending_actions(50)]
        if not actions:
            return 0

        llm_cfg = self._db.get_llm_config()
        executed = 0
        for action in actions:
            if self._shutdown.is_set():
                break

            worker = IMAPWorker(
                email_addr=action["email"],
                password=action["password"],
                imap_host=action["imap_host"],
                imap_port=action["imap_port"],
                smtp_host=action.get("smtp_host", ""),
                smtp_port=action.get("smtp_port", 587),
                proxy=action.get("proxy", ""),
                provider=action.get("provider", ""),
                user_agent=action.get("user_agent", ""),
                llm_config=llm_cfg,
            )

            if not worker.connect():
                self._db.mark_action_done(action["id"], "connect_failed")
                continue

            try:
                from_filter = action.get("message_id", "")
                atype = action["action_type"]

                if atype == "mark_read":
                    result = worker.mark_read(from_filter)
                elif atype == "rescue_spam":
                    result = worker.rescue_from_spam(from_filter)
                elif atype == "flag_important":
                    result = worker.flag_important(from_filter)
                elif atype == "click_links":
                    result = worker.click_links(from_filter)
                elif atype == "send_reply":
                    result = worker.send_reply(from_filter)
                else:
                    result = f"unknown_action:{atype}"

                self._db.mark_action_done(action["id"], result)
                executed += 1
                logger.info("Warmup action %s on %s: %s", atype, action["email"], result)
            except Exception as e:
                self._db.mark_action_done(action["id"], f"error:{e}")
                logger.error("Warmup action error %s on %s: %s", atype, action["email"], e)
            finally:
                worker.disconnect()

            time.sleep(random.uniform(1, 5))

        return executed


def _mime_supports_pdf() -> bool:
    """Prüft ob BulkMIMEBuilder.build_email die neuen pdf_bytes-Parameter akzeptiert."""
    try:
        from .bulk_mime_builder import BulkMIMEBuilder
        import inspect
        sig = inspect.signature(BulkMIMEBuilder.build_email)
        return "pdf_bytes" in sig.parameters
    except Exception:
        return False
