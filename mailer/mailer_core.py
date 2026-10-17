import logging
import sys
import time
import signal
import threading
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed

from .config_manager import ConfigManager
from .db_manager import DBManager
from .content_engine import ContentEngine
from .mime_builder import MIMEBuilder
from .smtp_worker import SMTPPool, SMTPWorker, SendResult
from .antifingerprint import AntiFingerprintEngine
from .advanced_antifingerprint import AdvancedAntiFingerprintEngine
from .image_manager import ImageManager
from .redirect_manager import RedirectManager
from .ui_console import UIConsole

try:
    from colorama import Fore, Style
except ImportError:
    class _D:
        def __getattr__(self, _):
            return ""
    Fore = _D()
    Style = _D()


class _AtomicCounter:
    def __init__(self):
        self._value = 0
        self._lock = threading.Lock()

    def next(self) -> int:
        with self._lock:
            v = self._value
            self._value += 1
            return v


class MailerCore:
    BATCH_SIZE = 200

    def __init__(self, config_path: str = "config.ini", overrides: dict = None):
        self._shutdown = threading.Event()
        self._config = ConfigManager(config_path)
        if overrides:
            for key, val in overrides.items():
                sec, opt = key.split(".", 1)
                if not self._config._parser.has_section(sec):
                    self._config._parser.add_section(sec)
                self._config._parser.set(sec, opt, str(val))
        self._setup_logging()

        self._db = DBManager(self._config.db_path)
        self._content = ContentEngine(
            html_dir=self._config.html_dir,
            attachments_dir=self._config.attachments_dir,
            spintax_dir=self._config.spintax_dir,
            names_file=self._config.names_file,
            subjects_file=self._config.subjects_file,
            alt_texts_file=self._config.alt_texts_file,
        )
        self._smtp_pool = SMTPPool(
            self._config.smtp_file,
            timeout=self._config.smtp_timeout,
            warmup_delay=self._config.warmup_delay,
            warmup_count=self._config.warmup_count,
            ignore_ssl_errors=self._config.ignore_ssl_errors,
            proxy_file=self._config.proxy_file,
            proxy_rotate_every=self._config.proxy_rotate_every,
        )
        self._worker = SMTPWorker(
            self._smtp_pool,
            normal_delay=self._config.normal_delay,
            provider_delay=self._config.provider_delay,
        )
        if self._config.advanced_antifingerprint:
            self._antifingerprint = AdvancedAntiFingerprintEngine(
                enable_classes=self._config.antifingerprint_classes,
                structure_variation=self._config.structure_variation,
            )
        else:
            self._antifingerprint = AntiFingerprintEngine(
                enable_classes=self._config.antifingerprint_classes,
            )
        self._image_mgr = ImageManager(
            enabled=self._config.image_api_enabled,
            cloud_name=self._config.cloudinary_cloud_name,
            api_key=self._config.cloudinary_api_key,
            api_secret=self._config.cloudinary_api_secret,
            logos_dir=self._config.logos_dir,
            mode=self._config.image_mode,
            quantize=self._config.image_quantize,
            downscale=self._config.image_downscale,
            max_colors=self._config.logo_max_colors,
            logo_rotate_every=self._config.logo_rotate_every,
        )
        self._redirect_mgr = RedirectManager(
            target_url=self._config.redirect_target_url,
            db_path=self._config.redirect_db_path,
            enabled=self._config.redirect_enabled,
            rotate_every=self._config.redirect_rotate_every,
            gen_threads=self._config.redirect_gen_threads,
        )
        self._send_counter = _AtomicCounter()
        self._ui = UIConsole()

        try:
            signal.signal(signal.SIGINT, self._handle_signal)
            if hasattr(signal, "SIGTERM"):
                signal.signal(signal.SIGTERM, self._handle_signal)
        except ValueError:
            pass

    def stop(self) -> None:
        self._shutdown.set()

    def force_stop(self) -> None:
        self._shutdown.set()
        self._db.reset_in_progress()

    @property
    def is_running(self) -> bool:
        return not self._shutdown.is_set()

    @staticmethod
    def _setup_logging() -> None:
        handler = logging.FileHandler("smtp_errors.log", encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        ))
        log = logging.getLogger("mailer.smtp")
        log.setLevel(logging.ERROR)
        log.addHandler(handler)

    def _handle_signal(self, signum, frame) -> None:
        print(f"\n{Fore.YELLOW}[!] Shutdown signal received. Finishing current batch...{Style.RESET_ALL}")
        self._shutdown.set()

    def _wait_for_schedule(self) -> None:
        schedule = self._config.schedule_time
        if not schedule:
            return
        try:
            target_time = datetime.strptime(schedule, "%H:%M").time()
        except ValueError:
            print(f"{Fore.RED}[!] Invalid schedule_time: {schedule} (use HH:MM){Style.RESET_ALL}")
            return
        now = datetime.now()
        target = datetime.combine(now.date(), target_time)
        if target <= now:
            target += timedelta(days=1)
        wait = (target - now).total_seconds()
        print(f"  Scheduled:        {Fore.CYAN}{target.strftime('%Y-%m-%d %H:%M')}{Style.RESET_ALL} (in {int(wait//3600)}h {int(wait%3600//60)}m)")
        while wait > 0 and not self._shutdown.is_set():
            self._shutdown.wait(timeout=min(wait, 10))
            wait = (target - datetime.now()).total_seconds()

    def run(self) -> None:
        self._print_banner()
        self._wait_for_schedule()

        if self._smtp_pool.total == 0:
            print(f"{Fore.RED}[!] No SMTP accounts loaded. Check {self._config.smtp_file}{Style.RESET_ALL}")
            sys.exit(1)
        print(f"  SMTP accounts: {Fore.GREEN}{self._smtp_pool.size}{Style.RESET_ALL} live / {self._smtp_pool.total} total")
        if self._smtp_pool.proxy_count > 0:
            print(f"  Proxies:          {Fore.GREEN}{self._smtp_pool.proxy_count}{Style.RESET_ALL} loaded"
                  + (f" (rotate every {self._config.proxy_rotate_every})" if self._config.proxy_rotate_every else ""))

        api_key = self._config.mxtoolbox_api_key
        if api_key:
            from .blacklist_checker import BlacklistChecker
            checker = BlacklistChecker(api_key)
            proxies = self._smtp_pool._proxies if self._smtp_pool.proxy_count > 0 else None
            print(f"  Blacklist check:  ", end="")
            results = checker.check_sending_ips(proxies)
            all_clean = True
            for label, info in results.items():
                if info["clean"]:
                    print(f"{Fore.GREEN}{label} OK{Style.RESET_ALL}  ", end="")
                else:
                    all_clean = False
                    print(f"{Fore.RED}{label} LISTED ({len(info['details'])} lists){Style.RESET_ALL}  ", end="")
            print()
            if not all_clean:
                print(f"  {Fore.RED}[!] Some IPs are blacklisted. Deliverability may suffer.{Style.RESET_ALL}")

        if self._content.has_names:
            print(f"  Names pool:       {Fore.GREEN}loaded{Style.RESET_ALL}")
        else:
            print(f"  Names pool:       {Fore.YELLOW}config fallback{Style.RESET_ALL}")
        if self._content.has_subjects:
            print(f"  Subjects pool:    {Fore.GREEN}loaded{Style.RESET_ALL}")
        else:
            print(f"  Subjects pool:    {Fore.YELLOW}config fallback{Style.RESET_ALL}")
        if self._content.has_attachments:
            print(f"  Attachments:      {Fore.GREEN}enabled{Style.RESET_ALL}")
        else:
            print(f"  Attachments:      {Fore.YELLOW}disabled (no files){Style.RESET_ALL}")

        self._db.reset_in_progress()
        newly_loaded = self._db.load_leads(self._config.leads_file)
        counts = self._db.count_by_state()
        total_pending = counts.get(DBManager.STATE_PENDING, 0)
        total_all = self._db.total_count()
        print(
            f"  Leads DB: {total_all} total, "
            f"{Fore.CYAN}{total_pending} pending{Style.RESET_ALL}, "
            f"{counts.get(DBManager.STATE_SENT, 0)} sent, "
            f"{counts.get(DBManager.STATE_FAILED, 0)} failed"
        )
        if newly_loaded:
            print(f"  New leads appended this run: {newly_loaded}")

        if total_pending == 0:
            print(f"\n{Fore.YELLOW}[*] No pending leads. Delete {self._config.db_path} to restart.{Style.RESET_ALL}")
            return

        if self._image_mgr.enabled:
            self._image_mgr.prepare(total_pending)
            if self._image_mgr.mode == "cloudinary":
                self._content.set_logo_urls(self._image_mgr.urls)
                print(f"  Image pool:       {Fore.GREEN}{self._image_mgr.pool_size} URLs (cloudinary){Style.RESET_ALL}")
            else:
                print(f"  Image pool:       {Fore.GREEN}{self._image_mgr.pool_size} templates (CID inline){Style.RESET_ALL}")

        if self._redirect_mgr.enabled:
            self._redirect_mgr.prepare(total_pending)
            self._redirect_mgr.wait_ready()
            print(f"  Redirect pool:    {Fore.GREEN}{self._redirect_mgr.pool_size} links (rotate every 10){Style.RESET_ALL}")

        thread_count = min(self._config.thread_count, self._smtp_pool.size * 2)
        thread_count = max(thread_count, 1)
        print(f"  Threads: {Fore.GREEN}{thread_count}{Style.RESET_ALL}")

        test_recipients = self._config.test_recipients
        if test_recipients:
            print(f"\n{Fore.CYAN}[*] Sending test emails to {len(test_recipients)} recipients...{Style.RESET_ALL}")
            self._send_test_emails(test_recipients)
            if not self._config.schedule_time:
                try:
                    answer = input(f"\n{Fore.YELLOW}[?] Test emails sent. Start mass send? [y/n]: {Style.RESET_ALL}").strip().lower()
                except EOFError:
                    answer = "y"
                if answer != "y":
                    print(f"{Fore.YELLOW}[*] Aborted by user.{Style.RESET_ALL}")
                    return

        print(f"\n{Fore.GREEN}[>] Starting mass send...{Style.RESET_ALL}\n")
        self._ui.start(total_pending)

        try:
            self._process_loop(thread_count)

            retried = self._db.retry_failed()
            if retried > 0 and not self._shutdown.is_set():
                print(f"\n{Fore.CYAN}[*] Retrying {retried} failed leads...{Style.RESET_ALL}")
                self._ui.stop()
                self._ui.start(retried)
                self._process_loop(thread_count)
        finally:
            self._ui.stop()
            self._ui.print_summary()
            self._db.reset_in_progress()
            self._db.close()

    def _wait_for_smtp(self) -> bool:
        while not self._shutdown.is_set():
            if self._smtp_pool.available_count > 0:
                return True
            if self._smtp_pool.all_dead:
                return False
            wait = self._smtp_pool.next_available_in()
            if wait < 0:
                return False
            self._shutdown.wait(timeout=min(wait, 5.0))
        return False

    def _process_loop(self, thread_count: int) -> None:
        with ThreadPoolExecutor(max_workers=thread_count) as executor:
            while not self._shutdown.is_set():
                if not self._wait_for_smtp():
                    print(f"\n{Fore.RED}[!] All SMTP servers permanently dead. Stopping.{Style.RESET_ALL}")
                    break

                batch = self._db.fetch_pending_batch(self.BATCH_SIZE)
                if not batch:
                    break

                lead_ids = [row[0] for row in batch]
                self._db.mark_in_progress(lead_ids)

                futures = {}
                for lead_id, email in batch:
                    if self._shutdown.is_set():
                        break
                    future = executor.submit(self._send_one, lead_id, email)
                    futures[future] = (lead_id, email)

                for future in as_completed(futures):
                    if self._shutdown.is_set():
                        break
                    lead_id, email = futures[future]
                    try:
                        result = future.result(timeout=120)
                    except Exception as exc:
                        result = SendResult(SendResult.TRANSIENT, str(exc)[:500])

                    self._handle_result(lead_id, result)

    def _handle_result(self, lead_id: int, result: SendResult) -> None:
        if result.is_success:
            self._db.mark_sent(lead_id)
            self._ui.record_sent()
            self._maybe_send_interval_test()
        elif result.is_fatal:
            self._db.mark_failed(lead_id, result.error)
            self._ui.record_failed()
        else:
            self._db.requeue_pending(lead_id)

    def _maybe_send_interval_test(self) -> None:
        interval = self._config.test_interval
        if interval <= 0:
            return
        raw = self._config.get("test", "interval_recipients")
        if not raw:
            raw = ",".join(self._config.test_recipients)
        if not raw:
            return
        recipients = [r.strip() for r in raw.split(",") if r.strip()]
        count = self._send_counter._value
        if count > 0 and count % interval == 0:
            for r in recipients:
                self._send_one(-1, r)

    def _pick_from_name_template(self) -> str:
        if self._content.has_names:
            return self._content.get_random_name()
        return self._config.from_name

    def _pick_subject_template(self) -> str:
        if self._content.has_subjects:
            return self._content.get_random_subject()
        return self._config.subject

    def _send_one(self, lead_id: int, email: str) -> SendResult:
        if self._shutdown.is_set():
            return SendResult(SendResult.TRANSIENT, "Shutdown")

        account = self._smtp_pool.acquire()
        if account is None:
            return SendResult(SendResult.TRANSIENT, "No SMTP available")

        from_email = self._config.from_email or account.user

        from_name = self._content.process(self._pick_from_name_template(), email)
        subject = self._content.process(self._pick_subject_template(), email)

        html_template = self._content.get_random_html()
        if html_template is None:
            html_template = "<p>Hello {email_user},</p><p>This is your notification.</p>"
        html_body = self._content.process(html_template, email)

        send_idx = self._send_counter.next()
        if self._redirect_mgr.enabled:
            link = self._redirect_mgr.get_link(send_idx)
            html_body = html_body.replace("{RedirectLink}", link)
            subject = subject.replace("{RedirectLink}", link)

        inline_images = None
        if self._image_mgr.enabled and self._image_mgr.mode == "cid" and "{Logo}" in html_body:
            cid_result = self._image_mgr.get_cid_logo(send_idx)
            if cid_result:
                img_bytes, cid_local, mime_type = cid_result
                domain = from_email.split("@")[1] if "@" in from_email else "mail"
                cid = f"{cid_local}@{domain}"
                html_body = self._content.resolve_logo_tag(
                    html_body, f"cid:{cid}", self._image_mgr.logo_width,
                )
                inline_images = [(img_bytes, cid, mime_type)]

        html_body = self._antifingerprint.transform(html_body)
        plain_body = ContentEngine.html_to_plaintext(html_body)

        attachment = None
        if self._content.has_attachments:
            attachment = self._content.get_random_attachment()

        raw_msg = MIMEBuilder.build_email(
            from_name=from_name,
            from_email=from_email,
            to_email=email,
            subject=subject,
            html_body=html_body,
            plain_body=plain_body,
            attachment=attachment,
            inline_images=inline_images,
        )

        result = self._worker.send(from_email, email, raw_msg, account=account)

        if result.is_transient and not result.is_success:
            self._db.suspend_smtp(
                account.key, account.fail_count,
                account.suspended_until, result.error,
            )

        if result.is_success:
            delay = self._worker.get_delay(email)
            if delay > 0 and not self._shutdown.is_set():
                time.sleep(delay)

        return result

    def _send_test_emails(self, recipients: list) -> None:
        for recipient in recipients:
            recipient = recipient.strip()
            if not recipient:
                continue
            try:
                result = self._send_one(-1, recipient)
                if result.is_success:
                    print(f"    {Fore.GREEN}[OK]{Style.RESET_ALL} Test sent to {recipient}")
                else:
                    print(f"    {Fore.RED}[FAIL]{Style.RESET_ALL} Test to {recipient}: {result.error}")
            except Exception as exc:
                print(f"    {Fore.RED}[FAIL]{Style.RESET_ALL} Test to {recipient}: {exc}")

    @staticmethod
    def _print_banner() -> None:
        print(f"""
{Fore.CYAN}╔══════════════════════════════════════════════════╗
║           MASS MAILER v1.0                       ║
║           High-Performance Email Engine           ║
╚══════════════════════════════════════════════════╝{Style.RESET_ALL}
""")
