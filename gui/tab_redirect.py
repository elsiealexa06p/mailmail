import os
import sqlite3
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from .helpers import log_event

REDIRECT_DB = "redirects.db"


class RedirectTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self._generating = False
        self._build_ui()
        self._refresh_list()

    def _build_ui(self):
        gen = ttk.LabelFrame(self, text="Generate Redirects (Google Share)", padding=10)
        gen.pack(fill="x", padx=10, pady=5)

        r1 = ttk.Frame(gen)
        r1.pack(fill="x", pady=2)
        ttk.Label(r1, text="Target URL:").pack(side="left")
        self._url_var = tk.StringVar()
        ttk.Entry(r1, textvariable=self._url_var, width=50).pack(side="left", padx=5)

        r2 = ttk.Frame(gen)
        r2.pack(fill="x", pady=2)
        ttk.Label(r2, text="Count:").pack(side="left")
        self._count_var = tk.IntVar(value=100)
        ttk.Spinbox(r2, from_=1, to=5000, textvariable=self._count_var, width=8).pack(side="left", padx=5)
        ttk.Label(r2, text="Threads:").pack(side="left", padx=(15, 0))
        self._threads_var = tk.IntVar(value=3)
        ttk.Spinbox(r2, from_=1, to=10, textvariable=self._threads_var, width=5).pack(side="left", padx=5)

        r3 = ttk.Frame(gen)
        r3.pack(fill="x", pady=3)
        self._gen_btn = ttk.Button(r3, text="Generate", command=self._generate)
        self._gen_btn.pack(side="left", padx=5)
        self._gen_status = tk.StringVar(value="")
        ttk.Label(r3, textvariable=self._gen_status).pack(side="left", padx=10)

        self._gen_progress = ttk.Progressbar(gen, length=400, mode="determinate")
        self._gen_progress.pack(fill="x", pady=3)

        manual = ttk.LabelFrame(self, text="Add Manually", padding=8)
        manual.pack(fill="x", padx=10, pady=3)
        mf = ttk.Frame(manual)
        mf.pack(fill="x")
        self._manual_var = tk.StringVar()
        ttk.Entry(mf, textvariable=self._manual_var, width=55).pack(side="left", padx=5)
        ttk.Button(mf, text="Add", command=self._add_manual).pack(side="left", padx=3)
        ttk.Button(mf, text="Bulk Add", command=self._bulk_add).pack(side="left", padx=3)

        pool = ttk.LabelFrame(self, text="Redirect Pool", padding=5)
        pool.pack(fill="both", expand=True, padx=10, pady=5)

        cols = ("url", "created")
        self._tree = ttk.Treeview(pool, columns=cols, show="headings", height=10)
        self._tree.heading("url", text="Short URL")
        self._tree.heading("created", text="Created")
        self._tree.column("url", width=400)
        self._tree.column("created", width=160)
        sb = ttk.Scrollbar(pool, orient="vertical", command=self._tree.yview)
        self._tree.config(yscrollcommand=sb.set)
        self._tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        bf = ttk.Frame(self)
        bf.pack(fill="x", padx=10, pady=3)
        self._count_label = tk.StringVar(value="0 links")
        ttk.Label(bf, textvariable=self._count_label, font=("", 10, "bold")).pack(side="left")
        ttk.Button(bf, text="Refresh", command=self._refresh_list).pack(side="left", padx=10)
        ttk.Button(bf, text="Delete Selected", command=self._delete_sel).pack(side="left", padx=3)
        ttk.Button(bf, text="Clear All", command=self._clear_all).pack(side="left", padx=3)

    def _ensure_db(self):
        conn = sqlite3.connect(REDIRECT_DB, timeout=10)
        conn.execute("""CREATE TABLE IF NOT EXISTS redirect_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT, short_url TEXT NOT NULL,
            target_url TEXT NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
        conn.commit()
        return conn

    def _refresh_list(self):
        for item in self._tree.get_children():
            self._tree.delete(item)
        conn = self._ensure_db()
        rows = conn.execute("SELECT short_url, created_at FROM redirect_links ORDER BY id DESC").fetchall()
        conn.close()
        for url, created in rows:
            self._tree.insert("", "end", values=(url, created or ""))
        self._count_label.set(f"{len(rows)} links")

    def _add_manual(self):
        url = self._manual_var.get().strip()
        if not url:
            return
        conn = self._ensure_db()
        conn.execute("INSERT INTO redirect_links (short_url, target_url) VALUES (?, ?)", (url, "manual"))
        conn.commit()
        conn.close()
        self._manual_var.set("")
        self._refresh_list()

    def _bulk_add(self):
        win = tk.Toplevel(self)
        win.title("Bulk Add")
        win.geometry("500x300")
        ttk.Label(win, text="One URL per line:").pack(anchor="w", padx=10, pady=5)
        txt = tk.Text(win, height=12, font=("Consolas", 10))
        txt.pack(fill="both", expand=True, padx=10)

        def do_add():
            conn = self._ensure_db()
            added = 0
            for ln in txt.get("1.0", "end").strip().splitlines():
                ln = ln.strip()
                if ln:
                    conn.execute("INSERT INTO redirect_links (short_url, target_url) VALUES (?, ?)", (ln, "manual"))
                    added += 1
            conn.commit()
            conn.close()
            win.destroy()
            self._refresh_list()
            messagebox.showinfo("Done", f"Added {added} links")

        ttk.Button(win, text="Add All", command=do_add).pack(pady=5)

    def _delete_sel(self):
        sel = self._tree.selection()
        if not sel:
            return
        conn = self._ensure_db()
        for item in sel:
            url = self._tree.item(item, "values")[0]
            conn.execute("DELETE FROM redirect_links WHERE short_url = ?", (url,))
        conn.commit()
        conn.close()
        self._refresh_list()

    def _clear_all(self):
        if messagebox.askyesno("Clear", "Delete ALL redirect links?"):
            conn = self._ensure_db()
            conn.execute("DELETE FROM redirect_links")
            conn.commit()
            conn.close()
            self._refresh_list()

    def _generate(self):
        url = self._url_var.get().strip()
        if not url:
            messagebox.showwarning("URL", "Enter a target URL")
            return
        if not url.startswith("http"):
            url = "https://" + url
            self._url_var.set(url)
        if self._generating:
            return

        count = self._count_var.get()
        threads = self._threads_var.get()
        self._generating = True
        self._gen_btn.config(state="disabled")
        self._gen_progress["value"] = 0
        self._gen_progress["maximum"] = count
        log_event(f"Generating {count} redirects for {url}")

        def worker():
            from mailer.redirect_manager import RedirectManager

            def on_progress(done, total, result):
                self._gen_progress["value"] = done
                status = f"{done}/{total}"
                if result:
                    status += f" | last: {result[:40]}..."
                self._gen_status.set(status)

            results = RedirectManager.generate_batch_threaded(
                url, count, threads=threads, callback=on_progress
            )

            conn = self._ensure_db()
            for link in results:
                conn.execute("INSERT INTO redirect_links (short_url, target_url) VALUES (?, ?)", (link, url))
            conn.commit()
            conn.close()

            self._generating = False
            self._gen_btn.config(state="normal")
            self._gen_status.set(f"Done: {len(results)}/{count} generated")
            log_event(f"Redirects done: {len(results)}/{count}")
            self._refresh_list()

        threading.Thread(target=worker, daemon=True).start()
