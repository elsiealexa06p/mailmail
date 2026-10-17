"""HTML Preview + Raw MIME + Text:Image Ratio + Provider selection."""
import os
import json
import tempfile
import webbrowser
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
                                 QComboBox, QPushButton, QLabel, QTabWidget,
                                 QTextEdit, QMessageBox, QFormLayout, QLineEdit)
from PySide6.QtCore import Qt
from email.utils import formatdate


class PreviewTab(QWidget):
    def __init__(self, db):
        super().__init__()
        self.db = db
        layout = QVBoxLayout(self)

        sel = QGroupBox("Preview Settings")
        form = QFormLayout(sel)

        self.provider_cb = QComboBox()
        self.provider_cb.addItems(["generic", "ses", "sendgrid", "mailgun", "postmark"])
        form.addRow("SMTP Provider:", self.provider_cb)

        self.from_name = QLineEdit("Newsletter")
        form.addRow("From Name:", self.from_name)
        self.from_email = QLineEdit("newsletter@example.com")
        form.addRow("From Email:", self.from_email)
        self.to_email = QLineEdit("preview@example.com")
        form.addRow("To Email:", self.to_email)
        self.subject = QLineEdit("Test Subject")
        form.addRow("Subject:", self.subject)

        self.tmpl_cb = QComboBox()
        self.tmpl_cb.addItem("(none — use test HTML)", None)
        form.addRow("Template (optional):", self.tmpl_cb)

        layout.addWidget(sel)

        btns = QHBoxLayout()
        for text, fn in [("Refresh", self._refresh),
                         ("Preview HTML", self._preview_html),
                         ("Raw MIME Headers", self._preview_mime),
                         ("Full MIME Source", self._preview_full),
                         ("Text Ratio", self._ratio)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            btns.addWidget(b)
        layout.addLayout(btns)

        tabs = QTabWidget()
        self.html_view = QTextEdit()
        self.html_view.setReadOnly(True)
        tabs.addTab(self.html_view, "HTML Source")
        self.mime_view = QTextEdit()
        self.mime_view.setReadOnly(True)
        self.mime_view.setStyleSheet("font-family:Consolas; background:#1e1e1e; color:#d4d4d4")
        tabs.addTab(self.mime_view, "Raw MIME")
        self.info_view = QTextEdit()
        self.info_view.setReadOnly(True)
        tabs.addTab(self.info_view, "Info")
        layout.addWidget(tabs)

        self._refresh()

    def _refresh(self):
        current = self.tmpl_cb.currentData()
        self.tmpl_cb.blockSignals(True)
        self.tmpl_cb.clear()
        self.tmpl_cb.addItem("(none — use test HTML)", None)
        for t in self.db.get_templates():
            self.tmpl_cb.addItem(t["name"], t["id"])
        self.tmpl_cb.blockSignals(False)

    def _get_html(self):
        tid = self.tmpl_cb.currentData()
        if tid:
            tmpl = self.db._conn().execute(
                "SELECT * FROM message_templates WHERE id=?", (tid,)).fetchone()
            if tmpl:
                files = json.loads(tmpl["html_files_json"] or "[]")
                if files and os.path.isfile(files[0]):
                    with open(files[0], "r", encoding="utf-8") as f:
                        return f.read()
        return "<html><body><p>Hello {email_user},</p><p>This is a test newsletter.</p><p>Best regards</p></body></html>"

    def _build(self):
        from bulk.mailer.bulk_mime_builder import BulkMIMEBuilder
        from bulk.mailer.content_engine import BulkContentEngine

        macros = {r["name"]: json.loads(r["values_json"]) for r in self.db.get_macros()}
        engine = BulkContentEngine(macros)
        email = self.to_email.text().strip() or "preview@example.com"
        from_email = self.from_email.text().strip() or "newsletter@example.com"
        from_name = engine.process(self.from_name.text(), email)
        subject = engine.process(self.subject.text(), email)
        domain = from_email.split("@")[1] if "@" in from_email else "example.com"

        html = engine.process(self._get_html(), email)
        plain = engine.html_to_plaintext(html)
        provider = self.provider_cb.currentText()

        raw, envelope, tag = BulkMIMEBuilder.build_email(
            from_name=from_name, from_email=from_email,
            reply_to_name="", reply_to_email="",
            to_email=email, subject=subject,
            html_body=html, plain_body=plain,
            list_id_token=f"nl.{domain}", list_id_name="Newsletter",
            unsubscribe_url=f"https://unsub.{domain}/u/test",
            unsubscribe_mailto=f"unsub-test@{domain}",
            feedback_id=f"preview:test:p1:{domain.replace('.', '-')[:15]}",
            provider_type=provider,
        )

        raw = f"Date: {formatdate(localtime=True)}\r\n" + raw
        return html, raw

    def _preview_html(self):
        try:
            html, _ = self._build()
            self.html_view.setPlainText(html)
            with tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8") as f:
                f.write(html)
                webbrowser.open(f"file://{f.name}")
        except Exception as e:
            QMessageBox.warning(self, "Error", str(e))

    def _preview_mime(self):
        try:
            _, raw = self._build()
            headers_only = raw.split("\r\n\r\n")[0]
            self.mime_view.setPlainText(headers_only)
        except Exception as e:
            QMessageBox.warning(self, "Error", str(e))

    def _preview_full(self):
        try:
            _, raw = self._build()
            self.mime_view.setPlainText(raw[:15000])
        except Exception as e:
            QMessageBox.warning(self, "Error", str(e))

    def _ratio(self):
        try:
            html, raw = self._build()
            from bulk.mailer.content_engine import BulkContentEngine
            plain = BulkContentEngine.html_to_plaintext(html)
            text_bytes = len(plain.encode("utf-8"))
            total = len(raw.encode("utf-8"))
            text_pct = text_bytes / total * 100 if total else 0
            info = (f"Plain text: {text_bytes:,} bytes\n"
                    f"Total MIME: {total:,} bytes\n"
                    f"Text ratio: {text_pct:.0f}%\n\n")
            if text_pct >= 60:
                info += "GOOD — text-heavy"
            elif text_pct >= 30:
                info += "OK — balanced"
            else:
                info += "WARNING — too much non-text"
            self.info_view.setPlainText(info)
        except Exception as e:
            QMessageBox.warning(self, "Error", str(e))
