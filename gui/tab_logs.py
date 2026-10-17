import os
import tkinter as tk
from tkinter import ttk, messagebox

from .helpers import log_tail, db_path


class LogsTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self._auto_refresh = tk.BooleanVar(value=False)
        self._build_ui()
        self._refresh_log()

    def _build_ui(self):
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", padx=10, pady=5)
        ttk.Button(toolbar, text="Refresh", command=self._refresh_log).pack(side="left", padx=2)
        ttk.Button(toolbar, text="Clear Log", command=self._clear_log).pack(side="left", padx=2)
        ttk.Checkbutton(toolbar, text="Auto-refresh (3s)", variable=self._auto_refresh,
                        command=self._toggle_auto).pack(side="left", padx=10)

        ttk.Separator(toolbar).pack(side="left", fill="y", padx=10)
        ttk.Label(toolbar, text="Database:", font=("", 9, "bold")).pack(side="left", padx=5)
        ttk.Button(toolbar, text="Reset IN_PROGRESS", command=self._reset_db).pack(side="left", padx=2)
        ttk.Button(toolbar, text="Delete DB", command=self._delete_db).pack(side="left", padx=2)

        self._log_text = tk.Text(self, font=("Consolas", 10), wrap="word", state="disabled",
                                 bg="#1e1e1e", fg="#d4d4d4", insertbackground="white")
        sb = ttk.Scrollbar(self, orient="vertical", command=self._log_text.yview)
        self._log_text.config(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._log_text.pack(fill="both", expand=True, padx=10, pady=5)

    def _refresh_log(self):
        content = log_tail(100)
        self._log_text.config(state="normal")
        self._log_text.delete("1.0", "end")
        self._log_text.insert("1.0", content)
        self._log_text.config(state="disabled")
        self._log_text.see("end")

    def _clear_log(self):
        log_file = "smtp_errors.log"
        if os.path.isfile(log_file):
            open(log_file, "w").close()
        self._refresh_log()

    def _toggle_auto(self):
        if self._auto_refresh.get():
            self._auto_poll()

    def _auto_poll(self):
        if self._auto_refresh.get():
            self._refresh_log()
            self.after(3000, self._auto_poll)

    def _reset_db(self):
        try:
            from mailer.db_manager import DBManager
            db = DBManager(db_path())
            db.reset_in_progress()
            db.close()
            messagebox.showinfo("DB", "IN_PROGRESS reset to PENDING")
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _delete_db(self):
        path = db_path()
        if messagebox.askyesno("Delete DB", f"Delete {path}? This resets all progress."):
            if os.path.isfile(path):
                os.unlink(path)
                messagebox.showinfo("DB", "Database deleted")
