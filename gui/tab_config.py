import os
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from .helpers import read_config, save_config

SPINTAX_DIR = "spintaxes"

HELP = {
    ("sending", "threads"): "Number of parallel threads (1-200). More = faster but more server load.",
    ("sending", "normal_delay"): "Seconds between emails for normal domains. Gaussian jitter applied.",
    ("sending", "provider_delay"): "Seconds between emails for Gmail/Yahoo/Outlook (strict providers).",
    ("sending", "warmup_delay"): "Seconds between first N emails per SMTP (warmup phase).",
    ("sending", "warmup_count"): "How many emails until warmup is done per SMTP.",
    ("sending", "smtp_timeout"): "Connection timeout in seconds.",
    ("sending", "ignore_ssl_errors"): "true = accept self-signed certs. false = strict SSL validation.",
    ("sending", "schedule_time"): "Auto-start at this time (HH:MM). Empty = start immediately.",
    ("sending", "proxy_file"): "Path to proxies.txt. Format: ip:port:user:pass (one per line). Empty = no proxy.",
    ("sending", "proxy_rotate_every"): "Rotate to next proxy every N emails. 0 = stick to one proxy.",
    ("sending", "mxtoolbox_api_key"): "MXToolbox API key for blacklist checks. Empty = skip check.",
    ("sender", "from_name"): "Sender name. Use {from_name} to load from names.txt.",
    ("sender", "from_email"): "Sender email. Empty = uses SMTP account email.",
    ("sender", "subject"): "Subject line. Supports spintax, {tags}, [RANDSTR:...].",
    ("test", "test_recipients"): "Pre-check test emails (before mailing starts).",
    ("test", "interval_recipients"): "Interval test emails (during mailing). Falls back to test_recipients if empty.",
    ("test", "test_interval"): "Send interval test every N successful sends. 0 = disabled.",
    ("redirect", "rotate_every"): "Use same redirect link for N emails, then switch. Lower = more links needed.",
    ("redirect", "gen_threads"): "Parallel threads for redirect generation at startup (1-10).",
    ("content", "antifingerprint_classes"): "true = inject random CSS classes into HTML.",
    ("content", "advanced_antifingerprint"): "true = enable table-to-div structure transformation.",
    ("content", "structure_variation"): "0.0-1.0: probability of converting each table to divs.",
    ("IMAGE_API", "enabled"): "true = enable logo processing (CID or Cloudinary).",
    ("IMAGE_API", "mode"): "cid = embed logo in email. cloudinary = external URL.",
    ("IMAGE_API", "quantize"): "true = palette-compress logos (smaller). false = raw RGBA.",
    ("IMAGE_API", "downscale"): "true = resize to 220px. false = keep original pixels.",
    ("IMAGE_API", "logo_max_colors"): "Max palette colors (2-256). Lower = smaller file. 32 is good for flat logos.",
    ("IMAGE_API", "logo_rotate_every"): "Switch to next logo every N emails. 0 = use first logo only. All logos in /logos/ are used.",
    ("redirect", "enabled"): "true = enable redirect link rotation.",
    ("redirect", "target_url"): "The landing page URL for redirect generation.",
    ("redirect", "db_path"): "SQLite file for cached redirect links.",
    ("database", "db_path"): "SQLite file for lead tracking (delete to restart).",
}


class ConfigTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self._entries = {}
        self._build_ui()

    def _build_ui(self):
        canvas = tk.Canvas(self)
        scrollbar = ttk.Scrollbar(self, orient="vertical", command=canvas.yview)
        scroll_frame = ttk.Frame(canvas)
        scroll_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        # Mousewheel scrolling
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)

        self._inner = scroll_frame

        cp = read_config()
        row = 0
        scroll_frame.columnconfigure(0, weight=1)
        scroll_frame.columnconfigure(1, weight=1)

        proxy_lf = ttk.LabelFrame(scroll_frame, text="Proxy Settings", padding=8)
        proxy_lf.grid(row=row, column=0, columnspan=2, sticky="ew", padx=10, pady=5)
        row += 1

        ttk.Label(proxy_lf, text="Mode:").grid(row=0, column=0, sticky="w", padx=5)
        self._proxy_mode = tk.StringVar(value=self._detect_proxy_mode(cp))
        modes = ttk.Frame(proxy_lf)
        modes.grid(row=0, column=1, sticky="w", padx=5)
        ttk.Radiobutton(modes, text="Off", variable=self._proxy_mode, value="off").pack(side="left", padx=5)
        ttk.Radiobutton(modes, text="Single Proxy", variable=self._proxy_mode, value="single").pack(side="left", padx=5)
        ttk.Radiobutton(modes, text="Proxy List", variable=self._proxy_mode, value="list").pack(side="left", padx=5)

        ttk.Label(proxy_lf, text="Proxy/File:").grid(row=1, column=0, sticky="w", padx=5)
        self._proxy_val = tk.StringVar(value=cp.get("sending", "proxy_file", fallback=""))
        ttk.Entry(proxy_lf, textvariable=self._proxy_val, width=45).grid(row=1, column=1, sticky="w", padx=5, pady=2)
        ttk.Button(proxy_lf, text="Browse", command=self._browse_proxy).grid(row=1, column=2, padx=5)

        ttk.Label(proxy_lf, text="Rotate every:").grid(row=2, column=0, sticky="w", padx=5)
        self._proxy_rotate = tk.StringVar(value=cp.get("sending", "proxy_rotate_every", fallback="0"))
        rf = ttk.Frame(proxy_lf)
        rf.grid(row=2, column=1, sticky="w", padx=5)
        ttk.Entry(rf, textvariable=self._proxy_rotate, width=8).pack(side="left")
        ttk.Label(rf, text="emails (0 = no rotation)", foreground="gray").pack(side="left", padx=5)

        ttk.Label(proxy_lf, text="Formats: ip:port:user:pass  |  user:pass@ip:port  |  socks5://ip:port  |  ip:port",
                  foreground="gray", font=("", 8)).grid(row=3, column=0, columnspan=3, sticky="w", padx=5, pady=2)

        sections = [s for s in cp.sections() if s != "DEFAULT"]
        col = 0
        for si, section in enumerate(sections):
            lf = ttk.LabelFrame(scroll_frame, text=f"[{section}]", padding=8)
            lf.grid(row=row, column=col, sticky="nsew", padx=5, pady=5)
            col += 1
            if col >= 2:
                col = 0
                row += 1
            for i, (key, val) in enumerate(cp.items(section)):
                ttk.Label(lf, text=key, width=22, anchor="w").grid(row=i*2, column=0, sticky="w", padx=3, pady=1)
                var = tk.StringVar(value=val)
                entry = ttk.Entry(lf, textvariable=var, width=30)
                entry.grid(row=i*2, column=1, sticky="ew", padx=3, pady=1)
                self._entries[(section, key)] = var
                help_text = HELP.get((section, key), "")
                if help_text:
                    ttk.Label(lf, text=help_text, foreground="gray", font=("", 7),
                              wraplength=280).grid(row=i*2+1, column=0, columnspan=2, sticky="w", padx=3)
        if col == 1:
            row += 1

        btn_frame = ttk.Frame(scroll_frame)
        btn_frame.grid(row=row, column=0, columnspan=2, pady=10)
        ttk.Button(btn_frame, text="Save Config", command=self._save).pack(side="left", padx=5)
        ttk.Button(btn_frame, text="Reload", command=self._reload).pack(side="left", padx=5)
        row += 1

        sf = ttk.LabelFrame(scroll_frame, text="Spintax Files", padding=8)
        sf.grid(row=row, column=0, columnspan=2, sticky="ew", padx=10, pady=5)

        top = ttk.Frame(sf)
        top.pack(fill="x")
        ttk.Label(top, text="File:").pack(side="left")
        self._spin_var = tk.StringVar()
        self._spin_cb = ttk.Combobox(top, textvariable=self._spin_var, state="readonly", width=25)
        self._spin_cb.pack(side="left", padx=5)
        self._spin_cb.bind("<<ComboboxSelected>>", self._load_spintax)
        ttk.Button(top, text="Refresh", command=self._refresh_spintax).pack(side="left", padx=2)
        ttk.Button(top, text="Save", command=self._save_spintax).pack(side="left", padx=2)
        ttk.Button(top, text="New...", command=self._new_spintax).pack(side="left", padx=2)

        self._spin_editor = tk.Text(sf, height=10, font=("Consolas", 10), wrap="word")
        self._spin_editor.pack(fill="both", expand=True, pady=5)
        self._refresh_spintax()

        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

    def _detect_proxy_mode(self, cp) -> str:
        val = cp.get("sending", "proxy_file", fallback="").strip()
        if not val:
            return "off"
        if os.path.isfile(val):
            return "list"
        if ":" in val:
            return "single"
        return "off"

    def _browse_proxy(self):
        path = filedialog.askopenfilename(filetypes=[("Text", "*.txt"), ("All", "*.*")])
        if path:
            self._proxy_val.set(path)
            self._proxy_mode.set("list")

    def _save(self):
        cp = read_config()
        for (sec, key), var in self._entries.items():
            if not cp.has_section(sec):
                cp.add_section(sec)
            cp.set(sec, key, var.get())

        mode = self._proxy_mode.get()
        if not cp.has_section("sending"):
            cp.add_section("sending")
        if mode == "off":
            cp.set("sending", "proxy_file", "")
            cp.set("sending", "proxy_rotate_every", "0")
        else:
            cp.set("sending", "proxy_file", self._proxy_val.get())
            cp.set("sending", "proxy_rotate_every", self._proxy_rotate.get())

        save_config(cp)
        messagebox.showinfo("Config", "Saved!")

    def _reload(self):
        cp = read_config()
        for (sec, key), var in self._entries.items():
            try:
                var.set(cp.get(sec, key, fallback=""))
            except Exception:
                pass
        self._proxy_val.set(cp.get("sending", "proxy_file", fallback=""))
        self._proxy_rotate.set(cp.get("sending", "proxy_rotate_every", fallback="0"))
        self._proxy_mode.set(self._detect_proxy_mode(cp))

    def _refresh_spintax(self):
        os.makedirs(SPINTAX_DIR, exist_ok=True)
        files = sorted(f for f in os.listdir(SPINTAX_DIR) if f.endswith(".txt"))
        self._spin_cb["values"] = files
        if files and not self._spin_var.get():
            self._spin_var.set(files[0])
            self._load_spintax()

    def _load_spintax(self, event=None):
        name = self._spin_var.get()
        if not name:
            return
        try:
            with open(os.path.join(SPINTAX_DIR, name), "r", encoding="utf-8") as f:
                self._spin_editor.delete("1.0", "end")
                self._spin_editor.insert("1.0", f.read())
        except OSError:
            pass

    def _save_spintax(self):
        name = self._spin_var.get()
        if not name:
            return
        with open(os.path.join(SPINTAX_DIR, name), "w", encoding="utf-8") as f:
            f.write(self._spin_editor.get("1.0", "end-1c"))
        messagebox.showinfo("Spintax", f"Saved {name}")

    def _new_spintax(self):
        win = tk.Toplevel(self)
        win.title("New Spintax File")
        win.geometry("300x100")
        ttk.Label(win, text="Filename (without .txt):").pack(pady=5)
        var = tk.StringVar()
        ttk.Entry(win, textvariable=var, width=30).pack()

        def create():
            name = var.get().strip()
            if name:
                open(os.path.join(SPINTAX_DIR, f"{name}.txt"), "w").close()
                win.destroy()
                self._refresh_spintax()
                self._spin_var.set(f"{name}.txt")

        ttk.Button(win, text="Create", command=create).pack(pady=5)
