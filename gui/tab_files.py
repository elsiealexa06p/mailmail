import os
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from .helpers import scan_files, count_lines, preview_lines

LEADS_DIR = "Leads"
SMTPS_DIR = "SMTPs"
LOGOS_DIR = "logos"

for _d in (LEADS_DIR, SMTPS_DIR, LOGOS_DIR):
    os.makedirs(_d, exist_ok=True)


class FilesTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self._build_ui()

    def _build_ui(self):
        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=10, pady=5)

        # Leads
        self._leads_frame, self._leads_list, self._leads_count = self._make_panel(
            paned, "Leads", LEADS_DIR, (".txt",))

        # SMTPs
        self._smtp_frame, self._smtp_list, self._smtp_count = self._make_panel(
            paned, "SMTPs", SMTPS_DIR, (".txt",))

        # Logos
        self._logo_frame, self._logo_list, self._logo_count = self._make_panel(
            paned, "Logos", LOGOS_DIR, (".png", ".jpg", ".jpeg", ".gif", ".webp"),
            show_preview=True)

        # Preview area
        pv = ttk.LabelFrame(self, text="File Preview", padding=5)
        pv.pack(fill="x", padx=10, pady=5)
        self._preview = tk.Text(pv, height=6, font=("Consolas", 9), state="disabled", bg="#f5f5f5")
        self._preview.pack(fill="x")

        self._refresh_all()

    def _make_panel(self, paned, title, folder, exts, show_preview=False):
        lf = ttk.LabelFrame(paned, text=title, padding=5)
        paned.add(lf, weight=1)

        count_var = tk.StringVar(value="0 files")
        ttk.Label(lf, textvariable=count_var, font=("", 9, "bold")).pack(anchor="w")

        listbox = tk.Listbox(lf, height=12, font=("Consolas", 9))
        listbox.pack(fill="both", expand=True)
        listbox.bind("<<ListboxSelect>>", lambda e: self._on_select(folder, listbox))

        bf = ttk.Frame(lf)
        bf.pack(fill="x", pady=3)
        ttk.Button(bf, text="Add", command=lambda: self._add(folder, exts)).pack(side="left", padx=2)
        ttk.Button(bf, text="Delete", command=lambda: self._delete(folder, listbox)).pack(side="left", padx=2)
        ttk.Button(bf, text="View", command=lambda: self._view(folder, listbox)).pack(side="left", padx=2)

        if show_preview:
            self._logo_preview = ttk.Label(lf, text="")
            self._logo_preview.pack(pady=3)
            listbox.bind("<<ListboxSelect>>", lambda e: self._preview_logo(folder, listbox))

        return lf, listbox, count_var

    def _refresh_all(self):
        self._refresh_one(LEADS_DIR, self._leads_list, self._leads_count, (".txt",))
        self._refresh_one(SMTPS_DIR, self._smtp_list, self._smtp_count, (".txt",))
        self._refresh_one(LOGOS_DIR, self._logo_list, self._logo_count,
                          (".png", ".jpg", ".jpeg", ".gif", ".webp"))

    def _refresh_one(self, folder, listbox, count_var, exts):
        listbox.delete(0, "end")
        files = scan_files(folder, exts)
        for f in files:
            path = os.path.join(folder, f)
            if f.lower().endswith(".txt"):
                n = count_lines(path)
                listbox.insert("end", f"{f}  ({n:,} lines)")
            else:
                sz = os.path.getsize(path)
                listbox.insert("end", f"{f}  ({sz:,} bytes)")
        count_var.set(f"{len(files)} files")

    def _on_select(self, folder, listbox):
        sel = listbox.curselection()
        if not sel:
            return
        name = listbox.get(sel[0]).split("  (")[0]
        path = os.path.join(folder, name)
        self._preview.config(state="normal")
        self._preview.delete("1.0", "end")
        self._preview.insert("1.0", preview_lines(path, 10))
        self._preview.config(state="disabled")

    def _add(self, folder, exts):
        if isinstance(exts, str):
            exts = (exts,)
        ftypes = [("Files", " ".join(f"*{e}" for e in exts))]
        paths = filedialog.askopenfilenames(filetypes=ftypes)
        for src in paths:
            dest = os.path.join(folder, os.path.basename(src))
            with open(src, "rb") as fi, open(dest, "wb") as fo:
                fo.write(fi.read())
        self._refresh_all()

    def _delete(self, folder, listbox):
        sel = listbox.curselection()
        if not sel:
            return
        name = listbox.get(sel[0]).split("  (")[0]
        if messagebox.askyesno("Delete", f"Delete {name}?"):
            os.unlink(os.path.join(folder, name))
            self._refresh_all()

    def _view(self, folder, listbox):
        sel = listbox.curselection()
        if not sel:
            return
        name = listbox.get(sel[0]).split("  (")[0]
        path = os.path.join(folder, name)
        win = tk.Toplevel(self)
        win.title(name)
        win.geometry("700x500")
        txt = tk.Text(win, font=("Consolas", 10), wrap="word")
        txt.pack(fill="both", expand=True)
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                txt.insert("1.0", f.read())
        except OSError:
            txt.insert("1.0", "(cannot read)")

    def _preview_logo(self, folder, listbox):
        sel = listbox.curselection()
        if not sel:
            return
        name = listbox.get(sel[0]).split("  (")[0]
        path = os.path.join(folder, name)
        self._on_select(folder, listbox)
        try:
            from PIL import Image, ImageTk
            img = Image.open(path)
            img.thumbnail((180, 120))
            photo = ImageTk.PhotoImage(img)
            self._logo_preview.config(image=photo, text="")
            self._logo_preview._photo = photo
        except Exception:
            self._logo_preview.config(text=f"({name})", image="")
