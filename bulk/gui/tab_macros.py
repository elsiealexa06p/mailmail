"""Macros management — create, edit, export, import."""
import json
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
                                 QGroupBox, QListWidget, QListWidgetItem,
                                 QTextEdit, QPushButton, QLineEdit, QLabel,
                                 QMessageBox, QFileDialog, QHeaderView, QSpinBox)
from PySide6.QtCore import Qt


class MacrosTab(QWidget):
    def __init__(self, db):
        super().__init__()
        self.db = db
        self._current_id = None
        layout = QHBoxLayout(self)

        # Left: macro list
        left = QGroupBox("Macros")
        ll = QVBoxLayout(left)
        self.macro_list = QListWidget()
        self.macro_list.itemClicked.connect(self._on_select)
        ll.addWidget(self.macro_list)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Name:"))
        self.name_input = QLineEdit()
        self.name_input.setPlaceholderText("macro_name")
        name_row.addWidget(self.name_input)
        ll.addLayout(name_row)

        bl = QHBoxLayout()
        for text, fn in [("New", self._new), ("Delete", self._delete), ("Refresh", self._refresh)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            bl.addWidget(b)
        ll.addLayout(bl)

        bl2 = QHBoxLayout()
        for text, fn in [("Export All", self._export), ("Import", self._import), ("Backup DB", self._backup)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            bl2.addWidget(b)
        ll.addLayout(bl2)

        # Right: editor
        right = QGroupBox("Values (one per line)")
        rl = QVBoxLayout(right)
        self.editor = QTextEdit()
        self.editor.setFont(self.editor.font())
        rl.addWidget(self.editor)

        self.info_label = QLabel("")
        rl.addWidget(self.info_label)

        rot_row = QHBoxLayout()
        rot_row.addWidget(QLabel("Rotate every:"))
        self.rotate_spin = QSpinBox()
        self.rotate_spin.setRange(0, 99999)
        self.rotate_spin.setSpecialValueText("Random (0)")
        rot_row.addWidget(self.rotate_spin)
        rot_row.addWidget(QLabel("emails (0 = random pick each time)"))
        rot_row.addStretch()
        rl.addLayout(rot_row)

        save_btn = QPushButton("Save Macro")
        save_btn.clicked.connect(self._save)
        rl.addWidget(save_btn)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([300, 600])
        layout.addWidget(splitter)

        self._refresh()

    def _refresh(self):
        self.macro_list.clear()
        for m in self.db.get_macros():
            vals = json.loads(m["values_json"])
            item = QListWidgetItem(f"{m['name']}  ({len(vals)} values)")
            item.setData(Qt.UserRole, m["id"])
            self.macro_list.addItem(item)

    def _on_select(self, item):
        mid = item.data(Qt.UserRole)
        self._current_id = mid
        row = self.db._conn().execute("SELECT * FROM macros WHERE id=?", (mid,)).fetchone()
        if row:
            self.name_input.setText(row["name"])
            vals = json.loads(row["values_json"])
            self.editor.setPlainText("\n".join(vals))
            self.rotate_spin.setValue(row["rotate_every"] if row["rotate_every"] else 0)
            self.info_label.setText(f"{len(vals)} values | Use as {{{{" + row['name'] + "}}}}")

    def _new(self):
        name = self.name_input.text().strip()
        if not name:
            QMessageBox.warning(self, "Name", "Enter a macro name")
            return
        try:
            self.db.add_macro(name, [])
            self._refresh()
        except Exception as e:
            QMessageBox.warning(self, "Error", str(e))

    def _save(self):
        if not self._current_id:
            QMessageBox.warning(self, "Select", "Select a macro first")
            return
        text = self.editor.toPlainText()
        values = [line.strip() for line in text.splitlines() if line.strip()]
        self.db.update_macro(self._current_id, values)
        name = self.name_input.text().strip()
        if name:
            self.db._conn().execute("UPDATE macros SET name=?, rotate_every=? WHERE id=?",
                                     (name, self.rotate_spin.value(), self._current_id))
            self.db._conn().commit()
        self._refresh()
        self.info_label.setText(f"Saved: {len(values)} values")

    def _delete(self):
        if not self._current_id:
            return
        if QMessageBox.question(self, "Delete", "Delete this macro?") == QMessageBox.Yes:
            self.db.delete_macro(self._current_id)
            self._current_id = None
            self.editor.clear()
            self._refresh()

    def _export(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export Macros", "macros.json",
                                               "JSON (*.json)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.db.export_macros())
            QMessageBox.information(self, "Export", f"Exported to {path}")

    def _import(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import Macros", "",
                                               "JSON (*.json)")
        if path:
            with open(path, "r", encoding="utf-8") as f:
                count = self.db.import_macros(f.read())
            QMessageBox.information(self, "Import", f"Imported {count} macros")
            self._refresh()

    def _backup(self):
        path, _ = QFileDialog.getSaveFileName(self, "Backup All Data", "bulk_backup.json",
                                               "JSON (*.json)")
        if path:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.db.export_all())
            QMessageBox.information(self, "Backup", f"Full backup saved to {path}")
