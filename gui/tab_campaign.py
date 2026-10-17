import time
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from .helpers import db_stats, scan_files, read_config, save_config, count_lines, preview_lines, log_event, get_events


class CampaignTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self._core = None
        self._thread = None
        self._running = False
        self._started_at = 0.0
        self._build_ui()
        self._poll()

    def _build_ui(self):
        left = ttk.Frame(self)
        left.pack(side="left", fill="both", expand=True, padx=5, pady=5)
        right = ttk.Frame(self, width=350)
        right.pack(side="right", fill="y", padx=5, pady=5)
        right.pack_propagate(False)

        # --- LEFT: File Selection + Control + Stats ---
        sel = ttk.LabelFrame(left, text="Mailing Source", padding=8)
        sel.pack(fill="x", pady=3)

        ttk.Label(sel, text="Leads:").grid(row=0, column=0, sticky="w")
        self._leads_var = tk.StringVar()
        self._leads_cb = ttk.Combobox(sel, textvariable=self._leads_var, state="readonly", width=35)
        self._leads_cb.grid(row=0, column=1, padx=5, pady=2)
        self._leads_cb.bind("<<ComboboxSelected>>", self._on_leads_change)
        self._leads_info = ttk.Label(sel, text="", foreground="gray")
        self._leads_info.grid(row=0, column=2, padx=5)

        ttk.Label(sel, text="SMTPs:").grid(row=1, column=0, sticky="w")
        self._smtp_var = tk.StringVar()
        self._smtp_cb = ttk.Combobox(sel, textvariable=self._smtp_var, state="readonly", width=35)
        self._smtp_cb.grid(row=1, column=1, padx=5, pady=2)
        self._smtp_cb.bind("<<ComboboxSelected>>", self._on_smtp_change)
        self._smtp_info = ttk.Label(sel, text="", foreground="gray")
        self._smtp_info.grid(row=1, column=2, padx=5)

        ttk.Button(sel, text="Refresh", command=self._refresh_files).grid(row=0, column=3, rowspan=2, padx=5)

        # Test mail
        test_f = ttk.LabelFrame(left, text="Test Mail", padding=8)
        test_f.pack(fill="x", pady=3)

        r1 = ttk.Frame(test_f)
        r1.pack(fill="x", pady=1)
        ttk.Label(r1, text="Pre-check:").pack(side="left")
        cp = read_config()
        self._test_var = tk.StringVar(value=cp.get("test", "test_recipients", fallback=""))
        ttk.Entry(r1, textvariable=self._test_var, width=35).pack(side="left", padx=5)
        ttk.Button(r1, text="Send Test", command=self._test_mail).pack(side="left", padx=3)

        r2 = ttk.Frame(test_f)
        r2.pack(fill="x", pady=1)
        ttk.Label(r2, text="Interval to:").pack(side="left")
        self._interval_addr_var = tk.StringVar(value=cp.get("test", "interval_recipients", fallback=""))
        ttk.Entry(r2, textvariable=self._interval_addr_var, width=25).pack(side="left", padx=5)
        ttk.Label(r2, text="every").pack(side="left")
        self._interval_count_var = tk.StringVar(value=cp.get("test", "test_interval", fallback="0"))
        ttk.Entry(r2, textvariable=self._interval_count_var, width=6).pack(side="left", padx=3)
        ttk.Label(r2, text="mails").pack(side="left")

        ttk.Button(test_f, text="Save All", command=self._save_test_addr).pack(anchor="w", pady=2)

        # Control
        ctrl = ttk.LabelFrame(left, text="Control", padding=8)
        ctrl.pack(fill="x", pady=3)
        self._btn_start = ttk.Button(ctrl, text="▶ START", command=self._start)
        self._btn_start.pack(side="left", padx=5)
        self._btn_pause = ttk.Button(ctrl, text="⏸ PAUSE", command=self._pause, state="disabled")
        self._btn_pause.pack(side="left", padx=5)
        self._btn_stop = ttk.Button(ctrl, text="⏹ STOP", command=self._stop, state="disabled")
        self._btn_stop.pack(side="left", padx=5)

        sep = ttk.Separator(ctrl, orient="vertical")
        sep.pack(side="left", fill="y", padx=10)

        ttk.Button(ctrl, text="Pre-Generate Logos", command=self._pregen_logos).pack(side="left", padx=3)
        ttk.Button(ctrl, text="Pre-Generate Redirects", command=self._pregen_redirects).pack(side="left", padx=3)
        ttk.Button(ctrl, text="Blacklist Check", command=self._blacklist_check).pack(side="left", padx=3)

        # Scheduler
        sched = ttk.LabelFrame(left, text="Scheduler", padding=8)
        sched.pack(fill="x", pady=3)
        ttk.Label(sched, text="Start at (HH:MM):").pack(side="left")
        self._sched_var = tk.StringVar(value=read_config().get("sending", "schedule_time", fallback=""))
        ttk.Entry(sched, textvariable=self._sched_var, width=8).pack(side="left", padx=5)
        ttk.Button(sched, text="Schedule", command=self._schedule).pack(side="left", padx=5)
        ttk.Button(sched, text="Cancel", command=self._cancel_schedule).pack(side="left", padx=3)
        self._sched_status = tk.StringVar(value="")
        ttk.Label(sched, textvariable=self._sched_status, foreground="blue").pack(side="left", padx=10)

        self._status_var = tk.StringVar(value="Idle")
        ttk.Label(ctrl, textvariable=self._status_var, font=("", 10, "bold")).pack(side="right", padx=10)

        # Delivery Info
        info = ttk.LabelFrame(left, text="Delivery Info", padding=8)
        info.pack(fill="x", pady=3)

        self._progress = ttk.Progressbar(info, length=500, mode="determinate")
        self._progress.pack(fill="x", pady=3)
        self._pct_var = tk.StringVar(value="0%")
        ttk.Label(info, textvariable=self._pct_var, font=("", 9)).pack()

        metrics = ttk.Frame(info)
        metrics.pack(fill="x", pady=3)
        self._lbl = {}
        defs = [("Total", "0"), ("Sent", "0"), ("Failed", "0"), ("Pending", "0"),
                ("Speed", "-"), ("Elapsed", "-"), ("ETA", "-")]
        for i, (key, default) in enumerate(defs):
            ttk.Label(metrics, text=f"{key}:").grid(row=i//4, column=(i%4)*2, sticky="e", padx=(8,2))
            lbl = ttk.Label(metrics, text=default, font=("", 10, "bold"), width=10)
            lbl.grid(row=i//4, column=(i%4)*2+1, sticky="w")
            self._lbl[key] = lbl

        # Preview
        pv = ttk.LabelFrame(left, text="Lead Preview", padding=5)
        pv.pack(fill="both", expand=True, pady=3)
        self._preview_text = tk.Text(pv, height=4, font=("Consolas", 9), state="disabled", bg="#f5f5f5")
        self._preview_text.pack(fill="both", expand=True)

        # --- RIGHT: Events Log ---
        ev = ttk.LabelFrame(right, text="Events Log", padding=5)
        ev.pack(fill="both", expand=True)
        self._events_text = tk.Text(ev, font=("Consolas", 9), state="disabled",
                                     bg="#1e1e1e", fg="#d4d4d4", width=40)
        self._events_text.pack(fill="both", expand=True)

        self._refresh_files()

    def _refresh_files(self):
        leads = []
        smtps = []
        for d in ["Leads", "leads", "."]:
            if os.path.isdir(d):
                for f in scan_files(d):
                    path = os.path.join(d, f)
                    if path not in leads:
                        leads.append(path)
        for d in ["SMTPs", "smtps", "."]:
            if os.path.isdir(d):
                for f in scan_files(d):
                    path = os.path.join(d, f)
                    if path not in smtps:
                        smtps.append(path)
        cp = read_config()
        lf = cp.get("paths", "leads_file", fallback="leads.txt")
        sf = cp.get("paths", "smtp_file", fallback="smtps.txt")
        if os.path.isdir(lf):
            for f in scan_files(lf):
                path = os.path.join(lf, f)
                if path not in leads:
                    leads.append(path)
        elif os.path.isfile(lf) and lf not in leads:
            leads.insert(0, lf)
        if os.path.isdir(sf):
            for f in scan_files(sf):
                path = os.path.join(sf, f)
                if path not in smtps:
                    smtps.append(path)
        elif os.path.isfile(sf) and sf not in smtps:
            smtps.insert(0, sf)
        self._leads_cb["values"] = leads
        self._smtp_cb["values"] = smtps
        if leads:
            self._leads_var.set(leads[0])
            self._on_leads_change()
        if smtps:
            self._smtp_var.set(smtps[0])
            self._on_smtp_change()

    def _on_leads_change(self, event=None):
        path = self._leads_var.get()
        if os.path.isfile(path):
            n = count_lines(path)
            self._leads_info.config(text=f"({n:,} leads)")
            self._preview_text.config(state="normal")
            self._preview_text.delete("1.0", "end")
            self._preview_text.insert("1.0", preview_lines(path, 8))
            self._preview_text.config(state="disabled")
        else:
            self._leads_info.config(text="(not found)")

    def _on_smtp_change(self, event=None):
        path = self._smtp_var.get()
        if os.path.isfile(path):
            n = count_lines(path)
            self._smtp_info.config(text=f"({n} accounts)")
        else:
            self._smtp_info.config(text="(not found)")

    def _save_test_addr(self):
        cp = read_config()
        if not cp.has_section("test"):
            cp.add_section("test")
        cp.set("test", "test_recipients", self._test_var.get())
        cp.set("test", "interval_recipients", self._interval_addr_var.get())
        cp.set("test", "test_interval", self._interval_count_var.get())
        save_config(cp)

    def _test_mail(self):
        addr = self._test_var.get().strip()
        if not addr:
            messagebox.showwarning("Test", "Enter test email address")
            return
        self._save_test_addr()
        log_event(f"Sending test to {addr}")
        self._status_var.set("Sending test...")

        def send():
            from mailer.mailer_core import MailerCore
            max_retries = 3
            for attempt in range(1, max_retries + 1):
                try:
                    core = MailerCore(config_path="config.ini")
                    if core._image_mgr.enabled:
                        core._image_mgr.prepare(10)
                        if core._image_mgr.mode == "cloudinary":
                            core._content.set_logo_urls(core._image_mgr.urls)
                    result = core._send_one(-1, addr.split(",")[0].strip())
                    if result.is_success:
                        log_event(f"Test OK to {addr}")
                        self._status_var.set("Test sent!")
                        return
                    log_event(f"Test attempt {attempt} failed: {result.error}")
                except Exception as e:
                    log_event(f"Test attempt {attempt} error: {e}")
                time.sleep(2)
            self._status_var.set("Test failed after retries")
            log_event(f"Test FAILED to {addr} after {max_retries} attempts")

        threading.Thread(target=send, daemon=True).start()

    def _start(self):
        if self._running:
            return
        leads = self._leads_var.get()
        smtp = self._smtp_var.get()
        if not leads or not smtp:
            messagebox.showerror("Error", "Select leads and SMTP files")
            return
        from mailer.mailer_core import MailerCore
        overrides = {"paths.leads_file": leads, "paths.smtp_file": smtp}
        log_event("Mailing started")

        def run():
            try:
                self._core = MailerCore(config_path="config.ini", overrides=overrides)
                self._core.run()
                log_event("Mailing finished")
            except Exception as e:
                log_event(f"Mailing error: {e}")
                self._status_var.set(f"Error: {e}")
            finally:
                self._running = False
                self._core = None

        self._running = True
        self._started_at = time.time()
        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        self._btn_start.config(state="disabled")
        self._btn_pause.config(state="normal")
        self._btn_stop.config(state="normal")
        self._status_var.set("Running")

    def _pause(self):
        if self._core:
            self._core.stop()
            log_event("Mailing paused")
            self._status_var.set("Paused")
            self._btn_start.config(state="normal")
            self._btn_pause.config(state="disabled")

    def _stop(self):
        if self._core:
            self._core.force_stop()
        self._running = False
        log_event("Mailing stopped")
        self._status_var.set("Stopped")
        self._btn_start.config(state="normal")
        self._btn_pause.config(state="disabled")
        self._btn_stop.config(state="disabled")

    def _pregen_logos(self):
        leads = self._leads_var.get()
        dirs = ["Leads", "leads", ""]
        lead_count = 0
        for d in dirs:
            full = os.path.join(d, leads) if d else leads
            if os.path.isfile(full):
                lead_count = count_lines(full)
                break
        if lead_count == 0:
            messagebox.showwarning("Logos", "Load leads first")
            return
        log_event(f"Pre-generating logos for {lead_count} leads")

        def gen():
            from mailer.image_manager import ImageManager
            cp = read_config()
            mgr = ImageManager(
                enabled=True, logos_dir=cp.get("paths", "logos_dir", fallback="logos"),
                mode="cid", quantize=cp.get("IMAGE_API", "quantize", fallback="true").lower() in ("true","1","yes"),
                downscale=cp.get("IMAGE_API", "downscale", fallback="false").lower() in ("true","1","yes"),
            )
            mgr.prepare(lead_count)
            log_event(f"Logos ready: {mgr.pool_size} templates")
            self._status_var.set(f"Logos: {mgr.pool_size} ready")

        threading.Thread(target=gen, daemon=True).start()
        self._status_var.set("Generating logos...")

    def _pregen_redirects(self):
        leads = self._leads_var.get()
        dirs = ["Leads", "leads", ""]
        lead_count = 0
        for d in dirs:
            full = os.path.join(d, leads) if d else leads
            if os.path.isfile(full):
                lead_count = count_lines(full)
                break
        if lead_count == 0:
            messagebox.showwarning("Redirects", "Load leads first")
            return
        cp = read_config()
        url = cp.get("redirect", "target_url", fallback="")
        if not url:
            messagebox.showwarning("Redirects", "Set target_url in config [redirect]")
            return
        log_event(f"Pre-generating redirects for {lead_count} leads")

        def gen():
            from mailer.redirect_manager import RedirectManager
            mgr = RedirectManager(target_url=url, db_path="redirects.db", enabled=True)
            mgr.prepare(lead_count)
            mgr.wait_ready()
            log_event(f"Redirects ready: {mgr.pool_size} links")
            self._status_var.set(f"Redirects: {mgr.pool_size} ready")

        threading.Thread(target=gen, daemon=True).start()
        self._status_var.set("Generating redirects...")

    def _poll(self):
        s = db_stats()
        total = s["total"]
        sent = s["SENT"]
        failed = s["FAILED"]
        pending = s["PENDING"] + s["IN_PROGRESS"]
        processed = sent + failed

        self._lbl["Total"].config(text=f"{total:,}")
        self._lbl["Sent"].config(text=f"{sent:,}")
        self._lbl["Failed"].config(text=f"{failed:,}")
        self._lbl["Pending"].config(text=f"{pending:,}")

        pct = processed / total * 100 if total > 0 else 0
        self._progress["value"] = pct
        self._pct_var.set(f"{pct:.1f}%")

        elapsed = time.time() - self._started_at if self._running else 0
        speed = processed / elapsed if elapsed > 1 else 0
        self._lbl["Speed"].config(text=f"{speed:.1f}/s")
        m, sec = divmod(int(elapsed), 60)
        h, m = divmod(m, 60)
        self._lbl["Elapsed"].config(text=f"{h}:{m:02d}:{sec:02d}")
        remaining = (total - processed) / speed if speed > 0 else 0
        m, sec = divmod(int(remaining), 60)
        h, m = divmod(m, 60)
        self._lbl["ETA"].config(text=f"{h}:{m:02d}:{sec:02d}" if speed > 0 else "--:--")

        if not self._running and self._btn_start["state"] == "disabled":
            self._btn_start.config(state="normal")
            self._btn_pause.config(state="disabled")
            self._btn_stop.config(state="disabled")
            if self._status_var.get() == "Running":
                self._status_var.set("Finished")

        events = get_events()
        self._events_text.config(state="normal")
        self._events_text.delete("1.0", "end")
        self._events_text.insert("1.0", "\n".join(events[-50:]))
        self._events_text.see("end")
        self._events_text.config(state="disabled")

        self.after(2000, self._poll)

    def _schedule(self):
        time_str = self._sched_var.get().strip()
        if not time_str:
            messagebox.showwarning("Schedule", "Enter time in HH:MM format")
            return
        cp = read_config()
        if not cp.has_section("sending"):
            cp.add_section("sending")
        cp.set("sending", "schedule_time", time_str)
        save_config(cp)
        from datetime import datetime, timedelta
        try:
            target_time = datetime.strptime(time_str, "%H:%M").time()
            target = datetime.combine(datetime.now().date(), target_time)
            if target <= datetime.now():
                target += timedelta(days=1)
            diff = (target - datetime.now()).total_seconds()
            h, m = int(diff // 3600), int(diff % 3600 // 60)
            self._sched_status.set(f"Scheduled for {target.strftime('%Y-%m-%d %H:%M')} ({h}h {m}m)")
            log_event(f"Scheduled for {time_str}")
        except ValueError:
            messagebox.showerror("Schedule", "Invalid format. Use HH:MM")

    def _cancel_schedule(self):
        cp = read_config()
        if cp.has_section("sending"):
            cp.set("sending", "schedule_time", "")
            save_config(cp)
        self._sched_status.set("Cancelled")
        self._sched_var.set("")
        log_event("Schedule cancelled")

    def _blacklist_check(self):
        log_event("Running blacklist check on sending IPs...")
        self._status_var.set("Checking blacklists...")

        def check():
            cp = read_config()
            api_key = cp.get("sending", "mxtoolbox_api_key", fallback="")
            if not api_key:
                self._status_var.set("No MXToolbox API key in config")
                log_event("Blacklist check: no API key")
                return
            from mailer.blacklist_checker import BlacklistChecker
            from mailer.smtp_worker import ProxyConfig
            checker = BlacklistChecker(api_key)

            proxy_file = cp.get("sending", "proxy_file", fallback="")
            proxies = []
            if proxy_file and os.path.isfile(proxy_file):
                with open(proxy_file, "r") as f:
                    for line in f:
                        p = ProxyConfig.parse(line.strip())
                        if p:
                            proxies.append(p)

            results = checker.check_sending_ips(proxies if proxies else None)
            for label, info in results.items():
                if info["clean"]:
                    log_event(f"Blacklist: {label} — CLEAN")
                else:
                    names = ", ".join(d.get("name", "") for d in info["details"][:3])
                    log_event(f"Blacklist: {label} — LISTED on {len(info['details'])} lists ({names})")
            self._status_var.set("Blacklist check done")

        threading.Thread(target=check, daemon=True).start()


import os
