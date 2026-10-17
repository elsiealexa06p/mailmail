"""Mailing Lists management tab with search, filter, bulk actions."""
import os
import re
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
                                 QGroupBox, QListWidget, QListWidgetItem,
                                 QTableWidget, QTableWidgetItem, QPushButton,
                                 QLineEdit, QLabel, QFileDialog, QMessageBox,
                                 QHeaderView, QInputDialog, QCheckBox, QTextEdit,
                                 QComboBox)
from PySide6.QtCore import Qt

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


class ListsTab(QWidget):
    def __init__(self, db):
        super().__init__()
        self.db = db
        self._current_list_id = None
        layout = QHBoxLayout(self)

        # Left: list of lists
        left = QGroupBox("Mailing Lists")
        ll = QVBoxLayout(left)
        self.list_widget = QListWidget()
        self.list_widget.itemClicked.connect(self._on_list_select)
        ll.addWidget(self.list_widget)

        bl = QHBoxLayout()
        for text, fn in [("Import", self._import), ("Delete List", self._delete_list),
                         ("Refresh", self._refresh)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            bl.addWidget(b)
        ll.addLayout(bl)

        # Right: lead viewer
        right = QWidget()
        rl = QVBoxLayout(right)

        # Search bar
        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Search:"))
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Search emails...")
        self.search_input.returnPressed.connect(self._search)
        search_row.addWidget(self.search_input)
        search_btn = QPushButton("Search")
        search_btn.clicked.connect(self._search)
        search_row.addWidget(search_btn)
        rl.addLayout(search_row)

        # Exclude bar
        exclude_row = QHBoxLayout()
        exclude_row.addWidget(QLabel("Exclude domain:"))
        self.exclude_input = QLineEdit()
        self.exclude_input.setPlaceholderText("e.g. yahoo.de")
        exclude_row.addWidget(self.exclude_input)
        del_domain_btn = QPushButton("Delete all @domain")
        del_domain_btn.clicked.connect(self._delete_domain)
        exclude_row.addWidget(del_domain_btn)
        rl.addLayout(exclude_row)

        # Exclude Rules
        rules_box = QGroupBox("Global Exclude Rules (applied to all lists unless disabled)")
        rules_l = QVBoxLayout(rules_box)
        self.exclude_rules = QTextEdit()
        self.exclude_rules.setMaximumHeight(80)
        self.exclude_rules.setPlaceholderText(
            "One rule per line. Prefix with @ for domain, no prefix for local-part.\n"
            "Examples: @spam.com  spam@  datenschutz@  dsgvo@  noreply@  abuse@  postmaster@")
        self.exclude_rules.setPlainText(
            "@spam.com\n@junk.com\nspam@\ndatenschutz@\ndsgvo@\nnoreply@\nabuse@\npostmaster@\nmailer-daemon@")
        rules_l.addWidget(self.exclude_rules)
        rl.addWidget(rules_box)

        # Table
        self.lead_table = QTableWidget()
        self.lead_table.setColumnCount(3)
        self.lead_table.setHorizontalHeaderLabels(["ID", "Email", "State"])
        self.lead_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.lead_table.setSelectionBehavior(QTableWidget.SelectRows)
        rl.addWidget(self.lead_table)

        # Stats + actions
        stats = QHBoxLayout()
        self.stats_label = QLabel("Select a list")
        stats.addWidget(self.stats_label)
        stats.addStretch()
        del_sel_btn = QPushButton("Delete Selected")
        del_sel_btn.clicked.connect(self._delete_selected)
        stats.addWidget(del_sel_btn)
        rl.addLayout(stats)

        # Compare lists
        cmp_box = QGroupBox("Compare Lists")
        cmp_l = QVBoxLayout(cmp_box)
        cmp_row = QHBoxLayout()
        cmp_row.addWidget(QLabel("List A:"))
        self.cmp_a = QComboBox()
        cmp_row.addWidget(self.cmp_a)
        cmp_row.addWidget(QLabel("List B:"))
        self.cmp_b = QComboBox()
        cmp_row.addWidget(self.cmp_b)
        cmp_btn = QPushButton("Compare")
        cmp_btn.clicked.connect(self._compare)
        cmp_row.addWidget(cmp_btn)
        cmp_l.addLayout(cmp_row)
        self.cmp_result = QTextEdit()
        self.cmp_result.setReadOnly(True)
        self.cmp_result.setMaximumHeight(100)
        cmp_l.addWidget(self.cmp_result)
        cmp_save_row = QHBoxLayout()
        save_only_a = QPushButton("Save 'Only in A' as new list")
        save_only_a.clicked.connect(lambda: self._save_compare("a"))
        cmp_save_row.addWidget(save_only_a)
        save_only_b = QPushButton("Save 'Only in B' as new list")
        save_only_b.clicked.connect(lambda: self._save_compare("b"))
        cmp_save_row.addWidget(save_only_b)
        cmp_l.addLayout(cmp_save_row)
        rl.addWidget(cmp_box)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([300, 700])
        layout.addWidget(splitter)

        self._refresh()

    def _refresh(self):
        self.list_widget.clear()
        self.cmp_a.clear()
        self.cmp_b.clear()
        for lst in self.db.get_lists():
            count = self.db.get_list_lead_count(lst["id"])
            label = f"{lst['name']}  ({count:,} leads)"
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, lst["id"])
            self.list_widget.addItem(item)
            self.cmp_a.addItem(lst["name"], lst["id"])
            self.cmp_b.addItem(lst["name"], lst["id"])

    def _get_exclude_rules(self) -> list:
        rules = []
        for line in self.exclude_rules.toPlainText().splitlines():
            line = line.strip().lower()
            if line:
                rules.append(line)
        return rules

    def _should_exclude(self, email: str, rules: list) -> bool:
        email = email.lower()
        local = email.split("@")[0] if "@" in email else ""
        domain = email.split("@")[1] if "@" in email else ""
        for rule in rules:
            if rule.startswith("@") and domain == rule[1:]:
                return True
            if rule.endswith("@") and local == rule[:-1]:
                return True
            if rule in email:
                return True
        return False

    def _import(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Import Leads", "",
                                                  "Text Files (*.txt *.csv);;All (*)")
        if not paths:
            return

        rules = self._get_exclude_rules()
        summary = []

        for path in paths:
            name = os.path.splitext(os.path.basename(path))[0]
            list_id = self.db.create_list(name, path)
            all_emails = []
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    for match in EMAIL_RE.findall(line):
                        all_emails.append(match.lower())
            filtered = [e for e in all_emails if not self._should_exclude(e, rules)]
            excluded_count = len(all_emails) - len(filtered)
            added = self.db.import_leads(list_id, filtered)
            summary.append(f"{name}: {added:,} leads" + (f" ({excluded_count} excluded)" if excluded_count else ""))

        self._refresh()
        QMessageBox.information(self, "Import Complete",
                                 f"{len(summary)} list(s) imported:\n\n" + "\n".join(summary))

    def _delete_list(self):
        item = self.list_widget.currentItem()
        if not item:
            return
        lid = item.data(Qt.UserRole)
        if QMessageBox.question(self, "Delete", f"Delete list '{item.text()}'?") == QMessageBox.Yes:
            self.db.delete_list(lid)
            self._current_list_id = None
            self._refresh()

    def _on_list_select(self, item):
        lid = item.data(Qt.UserRole)
        self._current_list_id = lid
        self._load_leads(lid)

    def _load_leads(self, list_id, query=""):
        if query:
            rows = self.db.search_leads(list_id, query)
        else:
            rows = self.db._conn().execute(
                "SELECT id, email, state FROM leads WHERE list_id=? LIMIT 1000",
                (list_id,)).fetchall()

        self.lead_table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self.lead_table.setItem(i, 0, QTableWidgetItem(str(r["id"])))
            self.lead_table.setItem(i, 1, QTableWidgetItem(r["email"]))
            self.lead_table.setItem(i, 2, QTableWidgetItem(r["state"]))

        stats = self.db.mailing_stats(list_id)
        self.stats_label.setText(
            f"Total: {stats['total']:,} | Pending: {stats['PENDING']:,} | "
            f"Sent: {stats['SENT']:,} | Failed: {stats['FAILED']:,} | "
            f"Excluded: {stats.get('EXCLUDED', 0):,}")

    def _search(self):
        if not self._current_list_id:
            return
        self._load_leads(self._current_list_id, self.search_input.text())

    def _delete_domain(self):
        if not self._current_list_id:
            return
        domain = self.exclude_input.text().strip()
        if not domain:
            return
        deleted = self.db.delete_leads_by_domain(self._current_list_id, domain)
        QMessageBox.information(self, "Deleted", f"Removed {deleted:,} leads with @{domain}")
        self._load_leads(self._current_list_id)
        self._refresh()

    def _delete_selected(self):
        rows = set(idx.row() for idx in self.lead_table.selectedIndexes())
        if not rows:
            return
        ids = []
        for r in rows:
            item = self.lead_table.item(r, 0)
            if item:
                ids.append(int(item.text()))
        if ids and QMessageBox.question(self, "Delete", f"Delete {len(ids)} leads?") == QMessageBox.Yes:
            self.db.delete_leads_by_ids(ids)
            if self._current_list_id:
                self._load_leads(self._current_list_id)
            self._refresh()

    def _compare(self):
        a_id = self.cmp_a.currentData()
        b_id = self.cmp_b.currentData()
        if not a_id or not b_id:
            return
        if a_id == b_id:
            self.cmp_result.setPlainText("Same list selected.")
            return
        c = self.db._conn()
        a_emails = {r[0] for r in c.execute("SELECT email FROM leads WHERE list_id=?", (a_id,)).fetchall()}
        b_emails = {r[0] for r in c.execute("SELECT email FROM leads WHERE list_id=?", (b_id,)).fetchall()}
        only_a = a_emails - b_emails
        only_b = b_emails - a_emails
        both = a_emails & b_emails
        self._compare_only_a = only_a
        self._compare_only_b = only_b
        self.cmp_result.setPlainText(
            f"In both: {len(both):,}\n"
            f"Only in A ({self.cmp_a.currentText()}): {len(only_a):,}\n"
            f"Only in B ({self.cmp_b.currentText()}): {len(only_b):,}")

    def _save_compare(self, which):
        emails = self._compare_only_a if which == "a" else getattr(self, "_compare_only_b", set())
        if not emails:
            QMessageBox.information(self, "Empty", "No unique emails to save")
            return
        label = self.cmp_a.currentText() if which == "a" else self.cmp_b.currentText()
        name = f"only_in_{label}"
        list_id = self.db.create_list(name)
        self.db.import_leads(list_id, list(emails))
        QMessageBox.information(self, "Saved", f"Saved {len(emails):,} leads as '{name}'")
        self._refresh()
