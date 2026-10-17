import os
import base64
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

from .helpers import read_config, scan_files, open_html_in_browser, open_raw_in_browser, _esc

HTML_DIR = "html_bodies"


class EditorTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self):
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", padx=10, pady=5)

        ttk.Label(toolbar, text="Template:").pack(side="left")
        self._file_var = tk.StringVar()
        self._file_cb = ttk.Combobox(toolbar, textvariable=self._file_var, state="readonly", width=25)
        self._file_cb.pack(side="left", padx=5)
        self._file_cb.bind("<<ComboboxSelected>>", self._load_file)

        for text, cmd in [("Refresh", self._refresh), ("Save", self._save),
                          ("Save As...", self._save_as)]:
            ttk.Button(toolbar, text=text, command=cmd).pack(side="left", padx=2)

        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=8)

        for text, cmd in [("Preview Raw", self._preview_raw),
                          ("Preview Processed", self._preview_processed),
                          ("Preview + AF", self._preview_af),
                          ("Preview + Advanced AF", self._preview_advanced),
                          ("3-Way Diff", self._three_way_diff),
                          ("Raw MIME", self._show_mime),
                          ("Text:Image Ratio", self._check_ratio)]:
            ttk.Button(toolbar, text=text, command=cmd).pack(side="left", padx=2)

        self._editor = tk.Text(self, font=("Consolas", 11), wrap="none", undo=True)
        sy = ttk.Scrollbar(self, orient="vertical", command=self._editor.yview)
        sx = ttk.Scrollbar(self, orient="horizontal", command=self._editor.xview)
        self._editor.config(yscrollcommand=sy.set, xscrollcommand=sx.set)
        sy.pack(side="right", fill="y")
        sx.pack(side="bottom", fill="x")
        self._editor.pack(fill="both", expand=True, padx=10)

        self._refresh()

    def _refresh(self):
        os.makedirs(HTML_DIR, exist_ok=True)
        files = scan_files(HTML_DIR, (".html", ".htm"))
        self._file_cb["values"] = files
        if files and not self._file_var.get():
            self._file_var.set(files[0])
            self._load_file()

    def _load_file(self, event=None):
        name = self._file_var.get()
        if not name:
            return
        try:
            with open(os.path.join(HTML_DIR, name), "r", encoding="utf-8") as f:
                self._editor.delete("1.0", "end")
                self._editor.insert("1.0", f.read())
        except OSError as e:
            messagebox.showerror("Error", str(e))

    def _save(self):
        name = self._file_var.get()
        if not name:
            return self._save_as()
        with open(os.path.join(HTML_DIR, name), "w", encoding="utf-8") as f:
            f.write(self._editor.get("1.0", "end-1c"))
        messagebox.showinfo("Saved", f"Saved {name}")

    def _save_as(self):
        path = filedialog.asksaveasfilename(initialdir=HTML_DIR, defaultextension=".html",
                                             filetypes=[("HTML", "*.html")])
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self._editor.get("1.0", "end-1c"))
            self._refresh()

    def _get_ce(self):
        from mailer.content_engine import ContentEngine
        cp = read_config()
        return ContentEngine(
            html_dir=HTML_DIR, attachments_dir="",
            spintax_dir=cp.get("paths", "spintax_dir", fallback="spintaxes"),
            names_file=cp.get("paths", "names_file", fallback=""),
            subjects_file=cp.get("paths", "subjects_file", fallback=""),
        )

    def _inject_logo_base64(self, html: str) -> str:
        if "{Logo}" not in html and "cid:" not in html:
            return html
        cp = read_config()
        logos_dir = cp.get("paths", "logos_dir", fallback="logos")
        if not os.path.isdir(logos_dir):
            return html
        imgs = [f for f in os.listdir(logos_dir) if f.lower().endswith((".png", ".jpg", ".jpeg"))]
        if not imgs:
            return html
        path = os.path.join(logos_dir, imgs[0])
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        ext = path.rsplit(".", 1)[-1].lower()
        mime = "image/png" if ext == "png" else "image/jpeg"
        data_uri = f"data:{mime};base64,{b64}"
        import re
        html = re.sub(r'src="cid:[^"]*"', f'src="{data_uri}"', html)
        if "{Logo}" in html:
            ce = self._get_ce()
            html = ce.resolve_logo_tag(html, data_uri, 220)
        return html

    def _process(self, af_level=0):
        ce = self._get_ce()
        raw = self._editor.get("1.0", "end-1c")
        processed = ce.process(raw, "preview@example.com")
        if af_level >= 1:
            from mailer.antifingerprint import AntiFingerprintEngine
            processed = AntiFingerprintEngine(enable_classes=True).transform(processed)
        if af_level >= 2:
            from mailer.advanced_antifingerprint import AdvancedAntiFingerprintEngine
            processed = AdvancedAntiFingerprintEngine(enable_classes=True, structure_variation=0.5).transform(processed)
        return raw, processed

    def _preview_raw(self):
        try:
            html = self._editor.get("1.0", "end-1c")
            html = self._inject_logo_base64(html)
            open_html_in_browser(html)
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _preview_processed(self):
        try:
            _, html = self._process(0)
            html = self._inject_logo_base64(html)
            open_html_in_browser(html)
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _preview_af(self):
        try:
            _, html = self._process(1)
            html = self._inject_logo_base64(html)
            open_html_in_browser(html)
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _preview_advanced(self):
        try:
            _, html = self._process(2)
            html = self._inject_logo_base64(html)
            open_html_in_browser(html)
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _three_way_diff(self):
        try:
            ce = self._get_ce()
            raw = self._editor.get("1.0", "end-1c")
            v1 = ce.process(raw, "preview@example.com")
            from mailer.antifingerprint import AntiFingerprintEngine
            v2 = AntiFingerprintEngine(enable_classes=True).transform(v1)
            from mailer.advanced_antifingerprint import AdvancedAntiFingerprintEngine
            v3 = AdvancedAntiFingerprintEngine(enable_classes=True, structure_variation=0.5).transform(v1)
            diff = (
                "<html><head><style>"
                "body{font-family:Consolas,monospace;font-size:12px;display:flex;gap:10px;padding:10px;margin:0}"
                ".col{flex:1;border:1px solid #ccc;padding:10px;overflow:auto;background:#fafafa}"
                "h3{margin:0 0 8px;padding:5px;background:#333;color:#fff;font-size:13px}"
                "pre{white-space:pre-wrap;word-break:break-all;margin:0}"
                "</style></head><body>"
                f"<div class='col'><h3>1. Spintax Only</h3><pre>{_esc(v1)}</pre></div>"
                f"<div class='col'><h3>2. + AntiFingerprint</h3><pre>{_esc(v2)}</pre></div>"
                f"<div class='col'><h3>3. + Advanced AF</h3><pre>{_esc(v3)}</pre></div>"
                "</body></html>"
            )
            open_html_in_browser(diff)
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _show_mime(self):
        try:
            _, html = self._process(2)
            from mailer.content_engine import ContentEngine
            from mailer.mime_builder import MIMEBuilder
            cp = read_config()
            plain = ContentEngine.html_to_plaintext(html)
            from_email = cp.get("sender", "from_email", fallback="") or "noreply@example.com"
            raw = MIMEBuilder.build_email(
                from_name="Preview", from_email=from_email,
                to_email="preview@example.com",
                subject=cp.get("sender", "subject", fallback="Preview"),
                html_body=html, plain_body=plain,
            )
            open_raw_in_browser(raw)
        except Exception as e:
            messagebox.showerror("Error", str(e))

    def _check_ratio(self):
        try:
            _, html = self._process(2)
            html = self._inject_logo_base64(html)
            import re as _re
            from mailer.content_engine import ContentEngine
            plain = ContentEngine.html_to_plaintext(html)
            text_bytes = len(plain.encode("utf-8"))

            img_bytes = 0
            cp = read_config()
            logos_dir = cp.get("paths", "logos_dir", fallback="logos")
            if os.path.isdir(logos_dir):
                imgs = [f for f in os.listdir(logos_dir) if f.lower().endswith((".png",".jpg",".jpeg"))]
                if imgs:
                    img_bytes = os.path.getsize(os.path.join(logos_dir, imgs[0]))

            img_b64 = int(img_bytes * 1.37) if img_bytes else 0
            total = text_bytes + img_b64
            text_pct = text_bytes / total * 100 if total else 100
            img_pct = img_b64 / total * 100 if total else 0

            if text_pct >= 70:
                verdict = "GOOD — text-heavy, looks transactional"
                color = "#00aa00"
            elif text_pct >= 40:
                verdict = "OK — balanced, monitor deliverability"
                color = "#cc8800"
            else:
                verdict = "WARNING — image-heavy, spam filters may flag"
                color = "#cc0000"

            ratio_html = f"""<html><body style="font-family:Arial;padding:30px">
            <h2>Text-to-Image Ratio Analysis</h2>
            <table style="border-collapse:collapse;font-size:16px">
            <tr><td style="padding:8px;font-weight:bold">Plain text:</td><td style="padding:8px">{text_bytes:,} bytes ({text_pct:.0f}%)</td></tr>
            <tr><td style="padding:8px;font-weight:bold">Logo (base64):</td><td style="padding:8px">{img_b64:,} bytes ({img_pct:.0f}%)</td></tr>
            <tr><td style="padding:8px;font-weight:bold">Total:</td><td style="padding:8px">{total:,} bytes</td></tr>
            <tr><td style="padding:8px;font-weight:bold">Logo raw:</td><td style="padding:8px">{img_bytes/1024:.1f} KB</td></tr>
            </table>
            <h3 style="color:{color};margin-top:20px">{verdict}</h3>
            <p style="color:#666">Target: 60-80% text, 20-40% images for optimal deliverability.</p>
            </body></html>"""
            open_html_in_browser(ratio_html)
        except Exception as e:
            messagebox.showerror("Error", str(e))
