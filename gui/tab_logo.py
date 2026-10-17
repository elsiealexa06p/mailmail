import os
import io
import threading
import tkinter as tk
from tkinter import ttk, messagebox

from .helpers import read_config, log_event

LOGOS_DIR = "logos"


class LogoTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self):
        top = ttk.LabelFrame(self, text="Logo Source", padding=8)
        top.pack(fill="x", padx=10, pady=5)

        self._logo_files = []
        ttk.Label(top, text="Logos in /logos/:").pack(anchor="w")
        self._logo_list = tk.Listbox(top, height=4, font=("Consolas", 9))
        self._logo_list.pack(fill="x", pady=3)
        self._logo_list.bind("<<ListboxSelect>>", self._on_select)
        ttk.Button(top, text="Refresh", command=self._refresh).pack(anchor="w")

        self._img_label = ttk.Label(top, text="")
        self._img_label.pack(pady=5)

        gen = ttk.LabelFrame(self, text="Generate Variants", padding=8)
        gen.pack(fill="x", padx=10, pady=5)

        r1 = ttk.Frame(gen)
        r1.pack(fill="x", pady=2)
        ttk.Label(r1, text="Lead count:").pack(side="left")
        self._lead_count = tk.IntVar(value=1000)
        ttk.Entry(r1, textvariable=self._lead_count, width=10).pack(side="left", padx=5)
        ttk.Label(r1, text="→ Templates:").pack(side="left", padx=(10,0))
        self._tmpl_count = tk.StringVar(value="25")
        ttk.Label(r1, textvariable=self._tmpl_count, font=("", 10, "bold")).pack(side="left", padx=5)
        self._lead_count.trace_add("write", self._calc_templates)

        self._gen_btn = ttk.Button(gen, text="Generate & Preview Sizes", command=self._generate)
        self._gen_btn.pack(anchor="w", pady=5)
        self._gen_progress = ttk.Progressbar(gen, mode="indeterminate")
        self._gen_progress.pack(fill="x", pady=3)

        info = ttk.LabelFrame(self, text="Variant Info", padding=8)
        info.pack(fill="both", expand=True, padx=10, pady=5)

        self._info_text = tk.Text(info, font=("Consolas", 10), height=12, state="disabled", bg="#f5f5f5")
        self._info_text.pack(fill="both", expand=True)

        self._refresh()

    def _refresh(self):
        self._logo_list.delete(0, "end")
        os.makedirs(LOGOS_DIR, exist_ok=True)
        self._logo_files = []
        for f in sorted(os.listdir(LOGOS_DIR)):
            if f.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
                path = os.path.join(LOGOS_DIR, f)
                size = os.path.getsize(path)
                self._logo_files.append(path)
                self._logo_list.insert("end", f"{f}  ({size/1024:.1f} KB)")

    def _on_select(self, event=None):
        sel = self._logo_list.curselection()
        if not sel or sel[0] >= len(self._logo_files):
            return
        path = self._logo_files[sel[0]]
        try:
            from PIL import Image, ImageTk
            img = Image.open(path)
            self._img_label.config(text=f"{img.size[0]}x{img.size[1]}px, {img.mode}")
            img.thumbnail((250, 150))
            photo = ImageTk.PhotoImage(img)
            self._img_label.config(image=photo)
            self._img_label._photo = photo
        except Exception:
            self._img_label.config(text="(cannot preview)", image="")

    def _calc_templates(self, *args):
        try:
            n = self._lead_count.get()
            count = min(max(n // 50, 25), 500)
            self._tmpl_count.set(str(count))
        except (tk.TclError, ValueError):
            pass

    def _generate(self):
        if not self._logo_files:
            messagebox.showwarning("Logo", "No logos in /logos/")
            return
        self._gen_btn.config(state="disabled")
        self._gen_progress.start()
        log_event("Generating logo variants...")

        def work():
            from mailer.image_manager import ImageManager
            cp = read_config()
            mgr = ImageManager(
                enabled=True, logos_dir=LOGOS_DIR, mode="cid",
                quantize=cp.get("IMAGE_API", "quantize", fallback="true").lower() in ("true","1","yes"),
                downscale=cp.get("IMAGE_API", "downscale", fallback="false").lower() in ("true","1","yes"),
                max_colors=int(cp.get("IMAGE_API", "logo_max_colors", fallback="256")),
            )
            lead_count = self._lead_count.get()
            mgr.prepare(lead_count)

            lines = []
            lines.append(f"Templates generated: {mgr.pool_size}")
            lines.append(f"Base logo: {self._logo_files[0]}")
            lines.append(f"Logo display width: {mgr.logo_width}px")
            lines.append("")

            sizes = []
            for i in range(min(20, mgr.pool_size)):
                result = mgr.get_cid_logo()
                if result:
                    sz = len(result[0])
                    sizes.append(sz)
                    lines.append(f"  Variant {i+1}: {sz/1024:.1f} KB")

            if sizes:
                avg = sum(sizes) / len(sizes)
                lines.append("")
                lines.append(f"Average: {avg/1024:.1f} KB")
                lines.append(f"Min: {min(sizes)/1024:.1f} KB")
                lines.append(f"Max: {max(sizes)/1024:.1f} KB")
                lines.append(f"Base64 overhead: ~{avg*1.37/1024:.1f} KB in MIME")

            self._info_text.config(state="normal")
            self._info_text.delete("1.0", "end")
            self._info_text.insert("1.0", "\n".join(lines))
            self._info_text.config(state="disabled")

            self._gen_progress.stop()
            self._gen_btn.config(state="normal")
            log_event(f"Logo variants ready: {mgr.pool_size}, avg {avg/1024:.1f}KB" if sizes else "No variants")

        threading.Thread(target=work, daemon=True).start()
