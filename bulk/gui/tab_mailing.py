"""Mailing control — multi-mailing with table, edit, schedule, test interval."""
import json
import time
import threading
from datetime import datetime, timedelta
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
                                 QComboBox, QPushButton, QLabel, QSpinBox,
                                 QLineEdit, QProgressBar, QTextEdit,
                                 QMessageBox, QFormLayout, QSplitter,
                                 QTableWidget, QTableWidgetItem, QHeaderView,
                                 QDialog, QDialogButtonBox, QTimeEdit)
from PySide6.QtCore import Qt, QTimer, QTime


class MailingTab(QWidget):
    def __init__(self, db):
        super().__init__()
        self.db = db
        self._threads = {}
        self._cores = {}
        layout = QVBoxLayout(self)

        top = QGroupBox("Mailings")
        tl = QVBoxLayout(top)
        btn_row = QHBoxLayout()
        for text, fn in [("Add...", self._add_mailing), ("Edit...", self._edit_mailing),
                         ("Start", self._start_selected), ("Stop", self._stop_selected),
                         ("Delete", self._delete_selected), ("Refresh", self._refresh_table)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            btn_row.addWidget(b)
        tl.addLayout(btn_row)

        self.table = QTableWidget()
        self.table.setColumnCount(8)
        self.table.setHorizontalHeaderLabels(
            ["Name", "Status", "Progress", "Total", "Sent", "Failed", "Schedule", "Test Every"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.itemClicked.connect(self._on_select)
        tl.addWidget(self.table)
        layout.addWidget(top)

        bottom = QSplitter(Qt.Horizontal)
        detail = QGroupBox("Mailing Details")
        dl = QVBoxLayout(detail)
        self.detail_text = QTextEdit()
        self.detail_text.setReadOnly(True)
        self.detail_text.setMaximumHeight(200)
        dl.addWidget(self.detail_text)
        bottom.addWidget(detail)

        events = QGroupBox("Events Log")
        el = QVBoxLayout(events)
        self.events_text = QTextEdit()
        self.events_text.setReadOnly(True)
        self.events_text.setStyleSheet("background:#1e1e2e; color:#cdd6f4; font-family:Consolas")
        el.addWidget(self.events_text)
        bottom.addWidget(events)
        layout.addWidget(bottom)

        self._poll_timer = QTimer()
        self._poll_timer.timeout.connect(self._refresh_table)
        self._poll_timer.start(3000)
        self._refresh_table()

    def _log(self, msg):
        self.events_text.append(f"{time.strftime('%H:%M:%S')}  {msg}")

    def _refresh_table(self):
        mailings = self.db.get_mailings()
        self.table.setRowCount(len(mailings))
        for i, m in enumerate(mailings):
            md = dict(m)
            total = md.get("total_leads", 0) or 0
            sent = md.get("sent", 0) or 0
            failed = md.get("failed", 0) or 0
            pct = f"{(sent+failed)/total*100:.0f}%" if total > 0 else "0%"
            schedule = md.get("schedule_time", "") or ""
            test_every = md.get("test_interval", 0) or 0

            for j, val in enumerate([
                md.get("name") or f"#{md['id']}", md.get("status", ""), pct,
                f"{total:,}", f"{sent:,}", f"{failed:,}",
                schedule, str(test_every) if test_every else "-"]):
                item = QTableWidgetItem(val)
                item.setData(Qt.UserRole, md["id"])
                self.table.setItem(i, j, item)

    def _get_selected_id(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return item.data(Qt.UserRole) if item else None

    def _on_select(self):
        mid = self._get_selected_id()
        if not mid:
            return
        m = self.db._conn().execute("SELECT * FROM mailings WHERE id=?", (mid,)).fetchone()
        if not m:
            return
        md = dict(m)
        info = "\n".join(f"{k}: {v}" for k, v in md.items())
        self.detail_text.setPlainText(info)

    def _mailing_dialog(self, existing=None):
        dlg = QDialog(self)
        dlg.setWindowTitle("Edit Mailing" if existing else "New Mailing")
        dlg.setMinimumWidth(550)
        fl = QFormLayout(dlg)

        name_input = QLineEdit()
        name_input.setPlaceholderText("e.g. KN-News.de April 2026")
        fl.addRow("Mailing Name:", name_input)

        brand_cb = QComboBox()
        domain_cb = QComboBox()
        list_cb = QComboBox()
        smtp_cb = QComboBox()
        tmpl_cb = QComboBox()
        sender_input = QLineEdit()
        sender_input.setPlaceholderText("Display name or {macro}")

        speed_group = QGroupBox("Send Speed")
        sg = QFormLayout(speed_group)
        daily_cb = QComboBox()
        daily_cb.addItems(["Use SMTP limit", "10,000/day", "25,000/day", "50,000/day",
                           "100,000/day", "500/hour", "1,000/hour", "5,000/hour", "Custom..."])
        daily_spin = QSpinBox()
        daily_spin.setRange(0, 999999)
        daily_spin.setSuffix(" mails/day")
        daily_spin.setVisible(False)
        daily_cb.currentTextChanged.connect(lambda t: daily_spin.setVisible(t == "Custom..."))
        sg.addRow("Speed:", daily_cb)
        sg.addRow("Custom:", daily_spin)

        exclude_input = QLineEdit()
        exclude_input.setPlaceholderText("yahoo.de, aol.com")
        test_email = QLineEdit()
        test_email.setPlaceholderText("test@inbox.com")
        test_interval = QSpinBox()
        test_interval.setRange(0, 999999)
        test_interval.setValue(1000)
        test_interval.setSpecialValueText("Disabled")
        schedule_input = QLineEdit()
        schedule_input.setPlaceholderText("HH:MM (empty = start immediately)")

        for b in self.db.get_brands():
            brand_cb.addItem(b["name"], b["id"])
        def on_brand():
            domain_cb.clear()
            bid = brand_cb.currentData()
            if bid:
                for d in self.db.get_domains(bid):
                    domain_cb.addItem(d["domain"], d["id"])
        brand_cb.currentIndexChanged.connect(on_brand)
        on_brand()

        for l in self.db.get_lists():
            cnt = self.db.get_list_lead_count(l["id"])
            list_cb.addItem(f"{l['name']} ({cnt:,})", l["id"])
        self.db.reset_daily_counts()
        for s in self.db.get_smtps():
            rem = self.db.get_smtp_remaining(s["id"])
            smtp_cb.addItem(f"{s['name']} ({rem:,} left)", s["id"])
        for t in self.db.get_templates():
            tmpl_cb.addItem(t["name"], t["id"])

        sender_row = QHBoxLayout()
        sender_row.addWidget(sender_input)
        macro_btn = QPushButton("Macro")
        def _insert_macro():
            from .macro_insert import insert_macro_into
            insert_macro_into(self.db, sender_input, dlg)
        macro_btn.clicked.connect(_insert_macro)
        sender_row.addWidget(macro_btn)

        fl.addRow("Brand:", brand_cb)
        fl.addRow("Domain:", domain_cb)
        fl.addRow("Sender Name:", sender_row)
        fl.addRow("Mailing List:", list_cb)
        fl.addRow("SMTP:", smtp_cb)
        fl.addRow("Template:", tmpl_cb)
        fl.addRow(speed_group)
        fl.addRow("Exclude Domains:", exclude_input)
        fl.addRow("Test Mail To:", test_email)
        fl.addRow("Test Every N:", test_interval)
        fl.addRow("Schedule (HH:MM):", schedule_input)

        if existing:
            name_input.setText(existing.get("name", ""))
            for i in range(brand_cb.count()):
                if brand_cb.itemData(i) == existing.get("brand_id"):
                    brand_cb.setCurrentIndex(i)
                    on_brand()
                    break
            for i in range(domain_cb.count()):
                if domain_cb.itemData(i) == existing.get("domain_id"):
                    domain_cb.setCurrentIndex(i)
                    break
            for i in range(list_cb.count()):
                if list_cb.itemData(i) == existing.get("list_id"):
                    list_cb.setCurrentIndex(i)
                    break
            for i in range(smtp_cb.count()):
                if smtp_cb.itemData(i) == existing.get("smtp_preset_id"):
                    smtp_cb.setCurrentIndex(i)
                    break
            for i in range(tmpl_cb.count()):
                if tmpl_cb.itemData(i) == existing.get("template_id"):
                    tmpl_cb.setCurrentIndex(i)
                    break
            exclude_input.setText(", ".join(json.loads(existing.get("exclude_domains_json", "[]"))))
            test_email.setText(existing.get("test_email", ""))
            test_interval.setValue(existing.get("test_interval", 0) or 0)
            schedule_input.setText(existing.get("schedule_time", ""))
            daily = existing.get("daily_limit", 0) or 0
            presets = {0: "Use SMTP limit", 10000: "10,000", 25000: "25,000",
                       50000: "50,000", 100000: "100,000"}
            if daily in presets:
                daily_cb.setCurrentText(presets[daily])
            else:
                daily_cb.setCurrentText("Custom...")
                daily_spin.setValue(daily)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        fl.addRow(btns)

        if dlg.exec() != QDialog.Accepted:
            return None

        limit_map = {"Use SMTP limit": 0, "10,000/day": 10000, "25,000/day": 25000,
                     "50,000/day": 50000, "100,000/day": 100000,
                     "500/hour": 12000, "1,000/hour": 24000, "5,000/hour": 120000,
                     "Custom...": daily_spin.value()}
        return {
            "name": name_input.text().strip() or f"Mailing {time.strftime('%Y-%m-%d')}",
            "brand_id": brand_cb.currentData(),
            "domain_id": domain_cb.currentData(),
            "list_id": list_cb.currentData(),
            "smtp_preset_id": smtp_cb.currentData(),
            "template_id": tmpl_cb.currentData(),
            "daily_limit": limit_map.get(daily_cb.currentText(), 0),
            "exclude_domains_json": json.dumps([d.strip() for d in exclude_input.text().split(",") if d.strip()]),
            "total_leads": self.db.get_list_lead_count(list_cb.currentData() or 0),
            "test_email": test_email.text().strip(),
            "test_interval": test_interval.value(),
            "schedule_time": schedule_input.text().strip(),
        }

    def _add_mailing(self):
        data = self._mailing_dialog()
        if not data:
            return
        mid = self.db.create_mailing(**data)
        self._log(f"Mailing #{mid} created")
        self._refresh_table()

    def _edit_mailing(self):
        mid = self._get_selected_id()
        if not mid:
            return
        if mid in self._cores:
            QMessageBox.warning(self, "Running", "Stop the mailing first")
            return
        row = self.db._conn().execute("SELECT * FROM mailings WHERE id=?", (mid,)).fetchone()
        if not row:
            return
        existing = dict(row)
        data = self._mailing_dialog(existing)
        if not data:
            return
        c = self.db._conn()
        sets = ", ".join(f"{k}=?" for k in data)
        c.execute(f"UPDATE mailings SET {sets} WHERE id=?", list(data.values()) + [mid])
        c.commit()
        self._log(f"Mailing #{mid} updated")
        self._refresh_table()

    def _start_selected(self):
        mid = self._get_selected_id()
        if not mid:
            return
        if mid in self._cores:
            QMessageBox.warning(self, "Running", "Already running")
            return

        row = self.db._conn().execute("SELECT * FROM mailings WHERE id=?", (mid,)).fetchone()
        if not row:
            return
        md = dict(row)
        schedule = md.get("schedule_time", "")

        from bulk.mailer.bulk_core import BulkMailerCore
        core = BulkMailerCore(self.db, mid)
        self._cores[mid] = core

        def run():
            if schedule:
                try:
                    target_time = datetime.strptime(schedule, "%H:%M").time()
                    now = datetime.now()
                    target = datetime.combine(now.date(), target_time)
                    if target <= now:
                        target += timedelta(days=1)
                    wait = (target - now).total_seconds()
                    self._log(f"Mailing #{mid} scheduled for {target.strftime('%H:%M')} ({int(wait//60)}m)")
                    while wait > 0 and mid in self._cores:
                        time.sleep(min(wait, 5))
                        wait = (target - datetime.now()).total_seconds()
                except ValueError:
                    pass
            try:
                core.run()
                self._log(f"Mailing #{mid} finished")
            except Exception as e:
                self._log(f"Mailing #{mid} ERROR: {e}")
            finally:
                self._cores.pop(mid, None)

        t = threading.Thread(target=run, daemon=True)
        t.start()
        self._threads[mid] = t
        self._log(f"Mailing #{mid} started" + (f" (scheduled {schedule})" if schedule else ""))
        self._refresh_table()

    def _stop_selected(self):
        mid = self._get_selected_id()
        if not mid:
            return
        core = self._cores.get(mid)
        if core:
            core.stop()
            self._cores.pop(mid, None)
            self._log(f"Mailing #{mid} stopped")
        self._refresh_table()

    def _delete_selected(self):
        mid = self._get_selected_id()
        if not mid:
            return
        if mid in self._cores:
            QMessageBox.warning(self, "Running", "Stop first")
            return
        if QMessageBox.question(self, "Delete", f"Delete mailing #{mid}?") == QMessageBox.Yes:
            c = self.db._conn()
            c.execute("DELETE FROM mailings WHERE id=?", (mid,))
            c.commit()
            self._refresh_table()
