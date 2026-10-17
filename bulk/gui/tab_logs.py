"""Logs + DB management + mailing history."""
import os
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QGroupBox,
                                 QTextEdit, QPushButton, QLabel, QTableWidget,
                                 QTableWidgetItem, QHeaderView, QMessageBox,
                                 QTabWidget, QCheckBox)
from PySide6.QtCore import Qt, QTimer


class LogsTab(QWidget):
    def __init__(self, db):
        super().__init__()
        self.db = db
        layout = QVBoxLayout(self)

        tabs = QTabWidget()

        # Log viewer
        log_tab = QWidget()
        ll = QVBoxLayout(log_tab)
        btns = QHBoxLayout()
        btns.addWidget(QPushButton("Refresh", clicked=self._refresh_log))
        btns.addWidget(QPushButton("Clear Log", clicked=self._clear_log))
        self.auto_refresh = QCheckBox("Auto-refresh (3s)")
        self.auto_refresh.stateChanged.connect(self._toggle_auto)
        btns.addWidget(self.auto_refresh)
        btns.addStretch()
        btns.addWidget(QPushButton("Reset IN_PROGRESS", clicked=self._reset_db))
        btns.addWidget(QPushButton("Delete DB", clicked=self._delete_db))
        ll.addLayout(btns)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setStyleSheet("font-family:Consolas; background:#1e1e1e; color:#d4d4d4")
        ll.addWidget(self.log_text)
        tabs.addTab(log_tab, "Error Log")

        # Mailing history
        hist_tab = QWidget()
        hl = QVBoxLayout(hist_tab)
        hl.addWidget(QPushButton("Refresh", clicked=self._refresh_history))
        self.hist_table = QTableWidget()
        self.hist_table.setColumnCount(7)
        self.hist_table.setHorizontalHeaderLabels(
            ["ID", "Status", "Sent", "Failed", "Excluded", "Started", "Finished"])
        self.hist_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        hl.addWidget(self.hist_table)
        tabs.addTab(hist_tab, "Mailing History")

        layout.addWidget(tabs)

        self._timer = QTimer()
        self._timer.timeout.connect(self._refresh_log)

        self._refresh_log()
        self._refresh_history()

    def _refresh_log(self):
        log_file = "smtp_errors.log"
        if not os.path.isfile(log_file):
            self.log_text.setPlainText("(no log file)")
            return
        try:
            with open(log_file, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            self.log_text.setPlainText("".join(lines[-100:]))
            self.log_text.verticalScrollBar().setValue(
                self.log_text.verticalScrollBar().maximum())
        except OSError:
            pass

    def _clear_log(self):
        if os.path.isfile("smtp_errors.log"):
            open("smtp_errors.log", "w").close()
        self._refresh_log()

    def _toggle_auto(self, state):
        if state:
            self._timer.start(3000)
        else:
            self._timer.stop()

    def _reset_db(self):
        self.db.reset_in_progress()
        QMessageBox.information(self, "DB", "IN_PROGRESS reset to PENDING")

    def _delete_db(self):
        if QMessageBox.question(self, "Delete", "Delete bulk.db?") == QMessageBox.Yes:
            if os.path.isfile("bulk.db"):
                os.unlink("bulk.db")
                QMessageBox.information(self, "DB", "Database deleted")

    def _refresh_history(self):
        rows = self.db.get_mailings()
        self.hist_table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self.hist_table.setItem(i, 0, QTableWidgetItem(str(r["id"])))
            self.hist_table.setItem(i, 1, QTableWidgetItem(r["status"]))
            self.hist_table.setItem(i, 2, QTableWidgetItem(str(r["sent"])))
            self.hist_table.setItem(i, 3, QTableWidgetItem(str(r["failed"])))
            self.hist_table.setItem(i, 4, QTableWidgetItem(str(r["excluded"])))
            self.hist_table.setItem(i, 5, QTableWidgetItem(str(r["started_at"] or "")))
            self.hist_table.setItem(i, 6, QTableWidgetItem(str(r["finished_at"] or "")))
