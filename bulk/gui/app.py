"""Bulk Mailer GUI — PySide6 Main Window with Sidebar."""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QHBoxLayout,
                                 QVBoxLayout, QListWidget, QListWidgetItem,
                                 QStackedWidget, QLabel)
from PySide6.QtCore import Qt, QSize
from PySide6.QtGui import QFont

from bulk.mailer.db_manager import BulkDBManager

from .tab_mailing import MailingTab
from .tab_brands import BrandsTab
from .tab_smtp import SMTPTab
from .tab_lists import ListsTab
from .tab_composer import ComposerTab
from .tab_macros import MacrosTab
from .tab_macro_help import MacroHelpTab
from .tab_preview import PreviewTab
from .tab_cloudflare import CloudflareTab
from .tab_logs import LogsTab


class BulkMailerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Bulk Mailer")
        self.setMinimumSize(1300, 800)
        self.db = BulkDBManager("bulk.db")

        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Sidebar
        sidebar = QWidget()
        sidebar.setFixedWidth(180)
        sidebar.setStyleSheet("""
            QWidget { background-color: #1e1e2e; }
            QListWidget { background-color: #1e1e2e; border: none; color: #cdd6f4;
                          font-size: 13px; padding-top: 10px; }
            QListWidget::item { padding: 12px 15px; border-left: 3px solid transparent; }
            QListWidget::item:selected { background-color: #313244;
                                         border-left: 3px solid #89b4fa; color: #ffffff; }
            QListWidget::item:hover { background-color: #2a2a3c; }
        """)
        sl = QVBoxLayout(sidebar)
        sl.setContentsMargins(0, 0, 0, 0)

        title = QLabel("  BULK MAILER")
        title.setStyleSheet("color: #89b4fa; font-size: 16px; font-weight: bold; padding: 15px 10px;")
        sl.addWidget(title)

        self.nav = QListWidget()
        self.nav.setIconSize(QSize(20, 20))
        pages = [
            "Mailings", "Brands", "SMTP", "Lists",
            "Composer", "Macros", "Macro Help",
            "Preview", "Cloudflare", "Logs",
        ]
        for name in pages:
            item = QListWidgetItem(name)
            self.nav.addItem(item)
        self.nav.currentRowChanged.connect(self._switch_page)
        sl.addWidget(self.nav)

        # Stacked pages
        self.stack = QStackedWidget()
        self.stack.addWidget(MailingTab(self.db))
        self.stack.addWidget(BrandsTab(self.db))
        self.stack.addWidget(SMTPTab(self.db))
        self.stack.addWidget(ListsTab(self.db))
        self.stack.addWidget(ComposerTab(self.db))
        self.stack.addWidget(MacrosTab(self.db))
        self.stack.addWidget(MacroHelpTab())
        self.stack.addWidget(PreviewTab(self.db))
        self.stack.addWidget(CloudflareTab(self.db))
        self.stack.addWidget(LogsTab(self.db))

        main_layout.addWidget(sidebar)
        main_layout.addWidget(self.stack)

        self.nav.setCurrentRow(0)

    def _switch_page(self, index):
        self.stack.setCurrentIndex(index)


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    win = BulkMailerWindow()
    win.show()
    sys.exit(app.exec())
