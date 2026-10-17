"""Brands & Domains management — Brand is just a name container."""
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
                                 QGroupBox, QTreeWidget, QTreeWidgetItem,
                                 QPushButton, QLineEdit, QLabel, QFormLayout,
                                 QMessageBox, QInputDialog, QHeaderView,
                                 QTableWidget, QTableWidgetItem, QTabWidget)
from PySide6.QtCore import Qt


class BrandsTab(QWidget):
    def __init__(self, db):
        super().__init__()
        self.db = db
        layout = QHBoxLayout(self)

        # Left: brand tree
        left = QGroupBox("Brands & Domains")
        ll = QVBoxLayout(left)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Name", "Type", "Info"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.tree.itemClicked.connect(self._on_select)
        ll.addWidget(self.tree)

        btns = QHBoxLayout()
        for text, fn in [("Add Brand", self._add_brand), ("Add Domain", self._add_domain),
                         ("Delete", self._delete), ("Refresh", self._refresh)]:
            b = QPushButton(text)
            b.clicked.connect(fn)
            btns.addWidget(b)
        ll.addLayout(btns)

        # Right: details
        right = QWidget()
        rl = QVBoxLayout(right)

        # Domain settings
        dom_box = QGroupBox("Domain Settings")
        df = QFormLayout(dom_box)
        self.edit_from_name = QLineEdit()
        self.edit_from_name.setPlaceholderText("Fixed name or {macro_name}")
        self.edit_from_email = QLineEdit()
        self.edit_reply = QLineEdit()
        self.edit_mail_sub = QLineEdit()
        self.edit_mail_sub.setPlaceholderText("mail (used for bounce + sending)")
        self.lbl_unsub = QLabel("Not deployed")
        df.addRow("From Name:", self.edit_from_name)
        df.addRow("From Email:", self.edit_from_email)
        df.addRow("Reply-To:", self.edit_reply)
        df.addRow("Mail Subdomain:", self.edit_mail_sub)
        df.addRow("Unsub Worker:", self.lbl_unsub)
        save_btn = QPushButton("Save Domain")
        save_btn.clicked.connect(self._save_domain)
        df.addRow(save_btn)

        deploy_btn = QPushButton("Deploy Unsub Worker for this Domain")
        deploy_btn.clicked.connect(self._deploy_unsub)
        df.addRow(deploy_btn)
        rl.addWidget(dom_box)

        # List usage overview
        tabs = QTabWidget()

        used_tab = QWidget()
        utl = QVBoxLayout(used_tab)
        self.used_table = QTableWidget()
        self.used_table.setColumnCount(2)
        self.used_table.setHorizontalHeaderLabels(["List Name", "Used At"])
        self.used_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        utl.addWidget(self.used_table)
        tabs.addTab(used_tab, "Used Lists")

        unused_tab = QWidget()
        uutl = QVBoxLayout(unused_tab)
        self.unused_table = QTableWidget()
        self.unused_table.setColumnCount(2)
        self.unused_table.setHorizontalHeaderLabels(["List Name", "Lead Count"])
        self.unused_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        uutl.addWidget(self.unused_table)
        mark_btn = QPushButton("Mark Selected as Used")
        mark_btn.clicked.connect(self._mark_used)
        uutl.addWidget(mark_btn)
        tabs.addTab(unused_tab, "Unused Lists")

        rl.addWidget(tabs)

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setSizes([400, 500])
        layout.addWidget(splitter)

        self._current_brand_id = None
        self._current_domain_id = None
        self._refresh()

    def _refresh(self):
        self.tree.clear()
        for brand in self.db.get_brands():
            item = QTreeWidgetItem([brand["name"], "Brand", ""])
            item.setData(0, Qt.UserRole, ("brand", brand["id"]))
            for dom in self.db.get_domains(brand["id"]):
                unsub = "✓ Unsub" if dom["unsub_worker_deployed"] else ""
                child = QTreeWidgetItem([dom["domain"], "Domain", unsub])
                child.setData(0, Qt.UserRole, ("domain", dom["id"], brand["id"]))
                item.addChild(child)
            item.setExpanded(True)
            self.tree.addTopLevelItem(item)

    def _add_brand(self):
        name, ok = QInputDialog.getText(self, "Add Brand", "Brand name:")
        if ok and name.strip():
            try:
                self.db.add_brand(name.strip())
                self._refresh()
            except Exception as e:
                QMessageBox.warning(self, "Error", str(e))

    def _add_domain(self):
        item = self.tree.currentItem()
        if not item:
            QMessageBox.warning(self, "Select", "Select a brand first")
            return
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        brand_id = data[1] if data[0] == "brand" else data[2] if len(data) > 2 else None
        if not brand_id:
            return
        domain, ok = QInputDialog.getText(self, "Add Domain", "Domain (e.g. news.example.com):")
        if ok and domain.strip():
            try:
                self.db.add_domain(brand_id, domain.strip())
                self._refresh()
            except Exception as e:
                QMessageBox.warning(self, "Error", str(e))

    def _delete(self):
        item = self.tree.currentItem()
        if not item:
            return
        data = item.data(0, Qt.UserRole)
        if not data:
            return
        if QMessageBox.question(self, "Delete", f"Delete '{item.text(0)}'?") != QMessageBox.Yes:
            return
        if data[0] == "brand":
            self.db.delete_brand(data[1])
        elif data[0] == "domain":
            self.db.delete_domain(data[1])
        self._refresh()

    def _on_select(self, item):
        data = item.data(0, Qt.UserRole)
        if not data:
            return

        if data[0] == "brand":
            self._current_brand_id = data[1]
            self._current_domain_id = None
            self._load_list_usage(data[1])

        elif data[0] == "domain":
            self._current_domain_id = data[1]
            self._current_brand_id = data[2] if len(data) > 2 else None
            row = self.db._conn().execute("SELECT * FROM domains WHERE id=?", (data[1],)).fetchone()
            if row:
                self.edit_from_name.setText(row["from_name"] or "")
                self.edit_from_email.setText(row["from_email"] or "")
                self.edit_reply.setText(row["reply_to_email"] or "")
                self.edit_mail_sub.setText(row["send_subdomain"] or "mail")
                if row["unsub_worker_deployed"]:
                    self.lbl_unsub.setText(f"✓ Deployed ({row['unsub_domain']})")
                else:
                    self.lbl_unsub.setText("Checking...")
                    import threading
                    threading.Thread(target=self._check_unsub_remote,
                                     args=(data[1], row["domain"]), daemon=True).start()
            if self._current_brand_id:
                self._load_list_usage(self._current_brand_id)

    def _save_domain(self):
        if not self._current_domain_id:
            QMessageBox.warning(self, "Select", "Select a domain")
            return
        c = self.db._conn()
        sub = self.edit_mail_sub.text() or "mail"
        c.execute("""UPDATE domains SET from_name=?, from_email=?, reply_to_email=?,
                     bounce_subdomain=?, send_subdomain=? WHERE id=?""",
                  (self.edit_from_name.text(), self.edit_from_email.text(),
                   self.edit_reply.text(), sub, sub, self._current_domain_id))
        c.commit()
        self._refresh()
        QMessageBox.information(self, "Saved", "Domain settings saved")

    def _check_unsub_remote(self, domain_id, domain):
        cf_accounts = self.db.get_cf_accounts()
        if not cf_accounts:
            self.lbl_unsub.setText("Not deployed")
            return
        acct = dict(cf_accounts[0])
        account_id = acct.get("account_id", "")
        if not account_id:
            self.lbl_unsub.setText("Not deployed")
            return
        if acct.get("global_api_key") and acct.get("auth_email"):
            headers = {"X-Auth-Key": acct["global_api_key"], "X-Auth-Email": acct["auth_email"]}
        else:
            headers = {"Authorization": f"Bearer {acct.get('api_token', '')}"}
        worker_name = f"unsub-{domain.replace('.', '-')}"
        try:
            import requests
            resp = requests.get(
                f"https://api.cloudflare.com/client/v4/accounts/{account_id}/workers/scripts/{worker_name}",
                headers=headers, timeout=10)
            if resp.status_code == 200:
                self.db.mark_unsub_deployed(domain_id, f"unsub.{domain}")
                self.lbl_unsub.setText(f"✓ Found on CF (unsub.{domain})")
            else:
                self.lbl_unsub.setText("Not deployed")
        except Exception:
            self.lbl_unsub.setText("Not deployed")

    def _deploy_unsub(self):
        if not self._current_domain_id:
            QMessageBox.warning(self, "Select", "Select a domain first")
            return
        row = self.db._conn().execute("SELECT * FROM domains WHERE id=?",
                                       (self._current_domain_id,)).fetchone()
        if not row:
            return
        domain = row["domain"]
        unsub_domain = f"unsub.{domain}"

        cf_accounts = self.db.get_cf_accounts()
        if not cf_accounts:
            QMessageBox.warning(self, "Cloudflare", "Add a Cloudflare account first (Cloudflare tab)")
            return

        acct = dict(cf_accounts[0])
        account_id = acct.get("account_id", "")
        if acct.get("global_api_key") and acct.get("auth_email"):
            headers = {"X-Auth-Key": acct["global_api_key"],
                       "X-Auth-Email": acct["auth_email"],
                       "Content-Type": "application/json"}
            upload_headers = {"X-Auth-Key": acct["global_api_key"],
                              "X-Auth-Email": acct["auth_email"]}
        else:
            token = acct.get("api_token", "")
            headers = {"Authorization": f"Bearer {token}",
                       "Content-Type": "application/json"}
            upload_headers = {"Authorization": f"Bearer {token}"}

        if not account_id:
            QMessageBox.warning(self, "Cloudflare", "Account ID missing")
            return

        import threading
        def deploy():
            import requests, json as j, os
            worker_name = f"unsub-{domain.replace('.', '-')}"
            kv_name = f"unsub-{domain.replace('.', '-')}"

            # Step 1: KV
            resp = requests.post(
                f"https://api.cloudflare.com/client/v4/accounts/{account_id}/storage/kv/namespaces",
                headers=headers, json={"title": kv_name}, timeout=30)
            if resp.status_code == 200 and resp.json().get("success"):
                ns_id = resp.json()["result"]["id"]
            else:
                ns_list = requests.get(
                    f"https://api.cloudflare.com/client/v4/accounts/{account_id}/storage/kv/namespaces",
                    headers=headers, timeout=30).json()
                ns_id = next((n["id"] for n in ns_list.get("result", []) if n["title"] == kv_name), None)
            if not ns_id:
                QMessageBox.warning(self, "Error", "Could not create KV namespace")
                return

            # Step 2: Worker
            worker_path = os.path.join(os.path.dirname(__file__), "..", "cloudflare", "unsubscribe-worker.js")
            if not os.path.isfile(worker_path):
                QMessageBox.warning(self, "Error", f"Worker script not found: {worker_path}")
                return
            with open(worker_path, "r") as f:
                code = f.read()
            metadata = j.dumps({"main_module": "worker.mjs", "compatibility_date": "2026-04-01",
                                 "bindings": [{"type": "kv_namespace", "name": "UNSUB_KV", "namespace_id": ns_id}]})
            resp = requests.put(
                f"https://api.cloudflare.com/client/v4/accounts/{account_id}/workers/scripts/{worker_name}",
                headers=upload_headers,
                files={"metadata": ("metadata.json", metadata, "application/json"),
                       "worker.mjs": ("worker.mjs", code, "application/javascript+module")},
                timeout=30)
            if not (resp.status_code == 200 and resp.json().get("success")):
                QMessageBox.warning(self, "Error", f"Worker deploy failed: {resp.text[:200]}")
                return

            # Step 3: Route
            zones = requests.get(f"https://api.cloudflare.com/client/v4/zones",
                                  headers=headers, params={"name": domain}, timeout=15).json()
            zone_id = None
            for z in zones.get("result", []):
                if z["name"] == domain:
                    zone_id = z["id"]
                    break
            if zone_id:
                requests.post(f"https://api.cloudflare.com/client/v4/zones/{zone_id}/workers/routes",
                              headers=headers,
                              json={"pattern": f"{unsub_domain}/*", "script": worker_name}, timeout=15)

            self.db.mark_unsub_deployed(self._current_domain_id, unsub_domain)
            self.lbl_unsub.setText(f"✓ Deployed ({unsub_domain})")
            self._refresh()
            QMessageBox.information(self, "Done", f"Unsub worker deployed for {unsub_domain}")

        threading.Thread(target=deploy, daemon=True).start()

    def _load_list_usage(self, brand_id):
        used = self.db.get_used_lists(brand_id)
        self.used_table.setRowCount(len(used))
        for i, r in enumerate(used):
            self.used_table.setItem(i, 0, QTableWidgetItem(r["name"]))
            self.used_table.setItem(i, 1, QTableWidgetItem(str(r.get("created_at", ""))))

        unused = self.db.get_unused_lists(brand_id)
        self.unused_table.setRowCount(len(unused))
        for i, r in enumerate(unused):
            item = QTableWidgetItem(r["name"])
            item.setData(Qt.UserRole, r["id"])
            self.unused_table.setItem(i, 0, item)
            count = self.db.get_list_lead_count(r["id"])
            self.unused_table.setItem(i, 1, QTableWidgetItem(f"{count:,}"))

    def _mark_used(self):
        if not self._current_brand_id:
            return
        rows = set(idx.row() for idx in self.unused_table.selectedIndexes())
        for r in rows:
            item = self.unused_table.item(r, 0)
            if item:
                list_id = item.data(Qt.UserRole)
                if list_id:
                    self.db.mark_list_used(self._current_brand_id, list_id)
        self._load_list_usage(self._current_brand_id)
