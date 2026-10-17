"""Reusable macro insert dialog — lets user pick a macro and insert {name} into a field."""
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QListWidget,
                                 QListWidgetItem, QPushButton, QLabel,
                                 QDialogButtonBox, QLineEdit)
from PySide6.QtCore import Qt


class MacroInsertDialog(QDialog):
    def __init__(self, db, parent=None):
        super().__init__(parent)
        self.db = db
        self.selected_macro = ""
        self.setWindowTitle("Insert Macro")
        self.setMinimumSize(400, 350)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Select a macro to insert:"))

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search...")
        self.search.textChanged.connect(self._filter)
        layout.addWidget(self.search)

        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(self._accept_item)
        layout.addWidget(self.list)

        self.preview = QLabel("")
        self.preview.setStyleSheet("color:gray; font-style:italic")
        layout.addWidget(self.preview)
        self.list.itemClicked.connect(self._on_select)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self._accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

        self._load()

    def _load(self):
        self.list.clear()
        for m in self.db.get_macros():
            import json
            vals = json.loads(m["values_json"])
            item = QListWidgetItem(f"{m['name']}  ({len(vals)} values)")
            item.setData(Qt.UserRole, m["name"])
            self.list.addItem(item)

    def _filter(self, text):
        for i in range(self.list.count()):
            item = self.list.item(i)
            item.setHidden(text.lower() not in item.text().lower())

    def _on_select(self, item):
        name = item.data(Qt.UserRole)
        import json
        vals = self.db.get_macro_values(name)
        preview = ", ".join(vals[:5])
        if len(vals) > 5:
            preview += f" ... (+{len(vals)-5})"
        self.preview.setText(f"Usage: {{{name}}} → {preview}")

    def _accept_item(self, item):
        self.selected_macro = f"{{{item.data(Qt.UserRole)}}}"
        self.accept()

    def _accept(self):
        item = self.list.currentItem()
        if item:
            self.selected_macro = f"{{{item.data(Qt.UserRole)}}}"
        self.accept()


def insert_macro_into(db, line_edit, parent=None):
    """Show macro dialog and insert selected macro at cursor position."""
    dlg = MacroInsertDialog(db, parent)
    if dlg.exec() == QDialog.Accepted and dlg.selected_macro:
        pos = line_edit.cursorPosition()
        text = line_edit.text()
        line_edit.setText(text[:pos] + dlg.selected_macro + text[pos:])
        line_edit.setCursorPosition(pos + len(dlg.selected_macro))
