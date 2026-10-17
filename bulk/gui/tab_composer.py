"""Message Composer — build message templates with HTML, PDF, rotation settings."""
import os
import json
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
                                 QGroupBox, QListWidget, QListWidgetItem,
                                 QPushButton, QLineEdit, QLabel, QFormLayout,
                                 QSpinBox, QCheckBox, QTextEdit, QFileDialog,
                                 QMessageBox, QComboBox)
from PySide6.QtCore import Qt


class ComposerTab(QWidget):
    def __init__(self, db):
        super().__init__()
        self.db = db
        self._current_id = None
        layout = QHBoxLayout(self)

        # Left: template list
        left = QGroupBox("Templates")
        ll = QVBoxLayout(left)
        self.tmpl_list = QListWidget()
        self.tmpl_list.itemClicked.connect(self._on_select)
        ll.addWidget(self.tmpl_list)

        bl = QHBoxLayout()
        for text, fn in [("New", self._new), ("Delete", self._delete), ("Refresh", self._refresh)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            bl.addWidget(b)
        ll.addLayout(bl)

        # Right: editor
        right = QWidget()
        rl = QVBoxLayout(right)

        form = QFormLayout()
        self.name_input = QLineEdit()
        form.addRow("Template Name:", self.name_input)

        # HTML files
        html_box = QGroupBox("HTML Bodies")
        hl = QVBoxLayout(html_box)
        self.html_list = QListWidget()
        self.html_list.setMaximumHeight(100)
        hl.addWidget(self.html_list)
        hb = QHBoxLayout()
        add_file_btn = QPushButton("Add File(s)")
        add_file_btn.clicked.connect(self._add_html)
        hb.addWidget(add_file_btn)
        add_folder_btn = QPushButton("Add Folder")
        add_folder_btn.clicked.connect(self._add_html_folder)
        hb.addWidget(add_folder_btn)
        add_code_btn = QPushButton("Paste HTML")
        add_code_btn.clicked.connect(self._paste_html)
        hb.addWidget(add_code_btn)
        remove_html_btn = QPushButton("Remove")
        remove_html_btn.clicked.connect(self._remove_html)
        hb.addWidget(remove_html_btn)
        hl.addLayout(hb)
        self.html_rotate = QSpinBox()
        self.html_rotate.setRange(0, 9999)
        self.html_rotate.setSpecialValueText("No rotation")
        hl.addWidget(QLabel("Rotate every N emails (0 = no rotation):"))
        hl.addWidget(self.html_rotate)

        # Subject
        subj_row = QHBoxLayout()
        self.subject_input = QLineEdit()
        self.subject_input.setPlaceholderText("Subject or {macro_name}")
        subj_row.addWidget(self.subject_input)
        insert_subj_btn = QPushButton("Insert Macro")
        insert_subj_btn.clicked.connect(self._insert_subject_macro)
        subj_row.addWidget(insert_subj_btn)
        form.addRow("Subject:", subj_row)

        # PDF
        pdf_box = QGroupBox("PDF Attachment")
        pl = QVBoxLayout(pdf_box)
        pdf_row = QHBoxLayout()
        self.pdf_path = QLineEdit()
        self.pdf_path.setPlaceholderText("No PDF")
        pdf_row.addWidget(self.pdf_path)
        browse_pdf_btn = QPushButton("Browse")
        browse_pdf_btn.clicked.connect(self._browse_pdf)
        pdf_row.addWidget(browse_pdf_btn)
        pl.addLayout(pdf_row)
        self.pdf_macro = QCheckBox("Enable PDF macro (random hash per mail)")
        pl.addWidget(self.pdf_macro)

        rl.addLayout(form)
        rl.addWidget(html_box)

        rl.addWidget(pdf_box)

        save_btn = QPushButton("Save Template")
        save_btn.clicked.connect(self._save)
        rl.addWidget(save_btn)
        rl.addStretch()

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([250, 750])
        layout.addWidget(splitter)

        self._refresh()

    def _refresh(self):
        self.tmpl_list.clear()
        for t in self.db.get_templates():
            item = QListWidgetItem(t["name"])
            item.setData(Qt.UserRole, t["id"])
            self.tmpl_list.addItem(item)

    def _on_select(self, item):
        tid = item.data(Qt.UserRole)
        self._current_id = tid
        row = self.db._conn().execute("SELECT * FROM message_templates WHERE id=?", (tid,)).fetchone()
        if not row:
            return
        self.name_input.setText(row["name"])
        self.subject_input.setText(row["subject_macro"] or "")
        self.html_rotate.setValue(row["html_rotate_every"] or 0)

        self.pdf_path.setText(row["pdf_path"] or "")
        self.pdf_macro.setChecked(bool(row["pdf_macro_enabled"]))

        self.html_list.clear()
        for f in json.loads(row["html_files_json"] or "[]"):
            self.html_list.addItem(f)



    def _new(self):
        name = self.name_input.text().strip() or "New Template"
        try:
            tid = self.db.add_template(name)
            self._current_id = tid
            self._refresh()
        except Exception as e:
            QMessageBox.warning(self, "Error", str(e))

    def _save(self):
        if not self._current_id:
            QMessageBox.warning(self, "Select", "Select or create a template")
            return

        html_files = [self.html_list.item(i).text() for i in range(self.html_list.count())]
        c = self.db._conn()
        c.execute("""UPDATE message_templates SET
                     name=?, html_files_json=?, html_rotate_every=?,
                     subject_macro=?, pdf_path=?, pdf_macro_enabled=? WHERE id=?""",
                  (self.name_input.text(), json.dumps(html_files),
                   self.html_rotate.value(), self.subject_input.text(),
                   self.pdf_path.text(), int(self.pdf_macro.isChecked()),
                   self._current_id))
        c.commit()
        self._refresh()
        QMessageBox.information(self, "Saved", "Template saved")

    def _delete(self):
        if not self._current_id:
            return
        if QMessageBox.question(self, "Delete", "Delete this template?") == QMessageBox.Yes:
            self.db.delete_template(self._current_id)
            self._current_id = None
            self._refresh()

    def _add_html(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Select HTML", "",
                                                  "HTML (*.html *.htm)")
        for p in paths:
            self.html_list.addItem(p)

    def _add_html_folder(self):
        from PySide6.QtWidgets import QFileDialog as QFD
        folder = QFD.getExistingDirectory(self, "Select HTML Folder")
        if folder:
            import os
            for f in sorted(os.listdir(folder)):
                if f.lower().endswith((".html", ".htm")):
                    self.html_list.addItem(os.path.join(folder, f))

    def _paste_html(self):
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QTextEdit, QDialogButtonBox
        dlg = QDialog(self)
        dlg.setWindowTitle("Paste HTML Code")
        dlg.setMinimumSize(600, 400)
        dl = QVBoxLayout(dlg)
        editor = QTextEdit()
        editor.setPlaceholderText("Paste your HTML code here...")
        dl.addWidget(editor)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        dl.addWidget(btns)

        if dlg.exec() == QDialog.Accepted:
            code = editor.toPlainText().strip()
            if code:
                import os, tempfile
                os.makedirs("bulk_html", exist_ok=True)
                import secrets
                name = f"pasted_{secrets.token_hex(4)}.html"
                path = os.path.join("bulk_html", name)
                with open(path, "w", encoding="utf-8") as f:
                    f.write(code)
                self.html_list.addItem(os.path.abspath(path))

    def _remove_html(self):
        row = self.html_list.currentRow()
        if row >= 0:
            self.html_list.takeItem(row)

    def _insert_subject_macro(self):
        from .macro_insert import insert_macro_into
        insert_macro_into(self.db, self.subject_input, self)

    def _browse_pdf(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select PDF", "",
                                               "PDF (*.pdf)")
        if path:
            self.pdf_path.setText(path)
