"""SMTP Presets management tab."""
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
                                 QTableWidget, QTableWidgetItem, QPushButton,
                                 QFormLayout, QLineEdit, QSpinBox, QMessageBox,
                                 QHeaderView, QDialog, QDialogButtonBox, QComboBox)
from PySide6.QtCore import Qt


class SMTPTab(QWidget):
    def __init__(self, db):
        super().__init__()
        self.db = db
        layout = QVBoxLayout(self)

        self.table = QTableWidget()
        self.table.setColumnCount(7)
        self.table.setHorizontalHeaderLabels(["Name", "Host", "Port", "Provider", "Username", "Daily Limit", "Sent Today"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        layout.addWidget(self.table)

        btns = QHBoxLayout()
        for text, fn in [("Add SMTP", self._add), ("Edit", self._edit),
                         ("Delete", self._delete), ("Reset Daily", self._reset),
                         ("Refresh", self._refresh)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            btns.addWidget(b)
        layout.addLayout(btns)

        self._refresh()

    def _refresh(self):
        self.db.reset_daily_counts()
        rows = self.db.get_smtps()
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            rd = dict(r)
            self.table.setItem(i, 0, QTableWidgetItem(rd.get("name", "")))
            self.table.setItem(i, 1, QTableWidgetItem(rd.get("host", "")))
            self.table.setItem(i, 2, QTableWidgetItem(str(rd.get("port", 587))))
            self.table.setItem(i, 3, QTableWidgetItem(rd.get("provider_type", "generic")))
            self.table.setItem(i, 4, QTableWidgetItem(rd.get("username", "")))
            self.table.setItem(i, 5, QTableWidgetItem(str(rd.get("daily_limit", 0))))
            self.table.setItem(i, 6, QTableWidgetItem(str(rd.get("sent_today", 0))))
            for j in range(7):
                item = self.table.item(i, j)
                if item:
                    item.setData(Qt.UserRole, rd["id"])

    def _get_selected_id(self):
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return item.data(Qt.UserRole) if item else None

    def _add(self):
        dlg = SMTPDialog(self)
        if dlg.exec() == QDialog.Accepted:
            d = dlg.get_data()
            self.db.add_smtp(**d)
            self._refresh()

    def _edit(self):
        sid = self._get_selected_id()
        if not sid:
            return
        row = self.db._conn().execute("SELECT * FROM smtp_presets WHERE id=?", (sid,)).fetchone()
        if not row:
            return
        dlg = SMTPDialog(self, dict(row))
        if dlg.exec() == QDialog.Accepted:
            d = dlg.get_data()
            c = self.db._conn()
            c.execute("UPDATE smtp_presets SET name=?,host=?,port=?,username=?,password=?,provider_type=?,daily_limit=?,proxy=? WHERE id=?",
                      (d["name"], d["host"], d["port"], d["username"], d["password"], d["provider_type"], d["daily_limit"], d["proxy"], sid))
            c.commit()
            self._refresh()

    def _delete(self):
        sid = self._get_selected_id()
        if not sid:
            return
        if QMessageBox.question(self, "Delete", "Delete this SMTP preset?") == QMessageBox.Yes:
            self.db.delete_smtp(sid)
            self._refresh()

    def _reset(self):
        self.db.reset_daily_counts()
        self._refresh()


class SMTPDialog(QDialog):
    def __init__(self, parent, existing=None):
        super().__init__(parent)
        self.setWindowTitle("SMTP Preset")
        self.setMinimumWidth(400)
        layout = QFormLayout(self)

        self.name = QLineEdit(existing["name"] if existing else "")
        self.host = QLineEdit(existing["host"] if existing else "")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(existing["port"] if existing else 587)
        self.username = QLineEdit(existing["username"] if existing else "")
        self.password = QLineEdit(existing["password"] if existing else "")
        self.password.setEchoMode(QLineEdit.Password)
        self.limit = QSpinBox()
        self.limit.setRange(0, 999999)
        self.limit.setValue(existing["daily_limit"] if existing else 50000)
        self.proxy = QLineEdit(existing["proxy"] if existing else "")
        self.proxy.setPlaceholderText("socks5://ip:port:user:pass (optional)")
        self.provider = QComboBox()
        self.provider.addItems(["generic", "ses", "sendgrid", "mailgun", "postmark"])
        if existing and existing.get("provider_type"):
            idx = self.provider.findText(existing["provider_type"])
            if idx >= 0:
                self.provider.setCurrentIndex(idx)

        layout.addRow("Name:", self.name)
        layout.addRow("Host:", self.host)
        layout.addRow("Port:", self.port)
        layout.addRow("Username:", self.username)
        layout.addRow("Password:", self.password)
        layout.addRow("Provider Type:", self.provider)
        layout.addRow("Daily Limit (0=unlimited):", self.limit)
        layout.addRow("Proxy:", self.proxy)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addRow(btns)

    def get_data(self) -> dict:
        return {
            "name": self.name.text(),
            "host": self.host.text(),
            "port": self.port.value(),
            "username": self.username.text(),
            "password": self.password.text(),
            "provider_type": self.provider.currentText(),
            "daily_limit": self.limit.value(),
            "proxy": self.proxy.text(),
        }
