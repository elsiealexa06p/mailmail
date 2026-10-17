"""Cloudflare Tab — R2 asset uploads + Worker deployment + Zone management."""
import os
import json
import threading
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QSplitter,
                                 QGroupBox, QTabWidget, QFormLayout, QLineEdit,
                                 QPushButton, QLabel, QComboBox, QListWidget,
                                 QListWidgetItem, QTextEdit, QFileDialog,
                                 QMessageBox, QProgressBar, QHeaderView,
                                 QTableWidget, QTableWidgetItem)
from PySide6.QtCore import Qt


class CloudflareTab(QWidget):
    def __init__(self, db):
        super().__init__()
        self.db = db
        self._r2 = None
        layout = QVBoxLayout(self)

        # Account Manager
        acct = QGroupBox("Cloudflare Accounts")
        al = QVBoxLayout(acct)
        self.acct_cb = QComboBox()
        self.acct_cb.currentIndexChanged.connect(self._on_account_change)
        al.addWidget(self.acct_cb)

        acct_btns = QHBoxLayout()
        add_acct = QPushButton("Add Account")
        add_acct.clicked.connect(self._add_account)
        acct_btns.addWidget(add_acct)
        del_acct = QPushButton("Delete")
        del_acct.clicked.connect(self._delete_account)
        acct_btns.addWidget(del_acct)
        connect_btn = QPushButton("Connect")
        connect_btn.clicked.connect(self._connect)
        acct_btns.addWidget(connect_btn)
        al.addLayout(acct_btns)

        self.status_label = QLabel("Not connected")
        al.addWidget(self.status_label)

        # Domains in this CF account vs brand domains
        domain_box = QGroupBox("Domains (from Cloudflare)")
        dl = QVBoxLayout(domain_box)
        self.cf_domain_list = QListWidget()
        dl.addWidget(self.cf_domain_list)
        sync_btn = QPushButton("Pull Domains from Cloudflare")
        sync_btn.clicked.connect(lambda: threading.Thread(target=self._pull_domains_bg, daemon=True).start())
        dl.addWidget(sync_btn)
        al.addWidget(domain_box)

        layout.addWidget(acct)

        # Tabs for R2 and Workers
        tabs = QTabWidget()

        # R2 Tab
        r2_tab = QWidget()
        r2l = QVBoxLayout(r2_tab)

        r2_top = QHBoxLayout()
        r2_top.addWidget(QLabel("Bucket:"))
        self.bucket_cb = QComboBox()
        self.bucket_cb.setMinimumWidth(200)
        r2_top.addWidget(self.bucket_cb)
        r2_top.addWidget(QPushButton("Refresh", clicked=self._refresh_buckets))
        r2_top.addWidget(QPushButton("Create Bucket", clicked=self._create_bucket))
        r2_top.addWidget(QPushButton("Enable Public", clicked=self._enable_public))
        r2_top.addStretch()
        r2l.addLayout(r2_top)

        domain_row = QHBoxLayout()
        domain_row.addWidget(QLabel("Custom Domain:"))
        self.custom_domain = QLineEdit()
        self.custom_domain.setPlaceholderText("cdn.yourdomain.com")
        domain_row.addWidget(self.custom_domain)
        domain_row.addWidget(QPushButton("Add Domain", clicked=self._add_custom_domain))
        r2l.addLayout(domain_row)

        # File list + upload
        r2_mid = QHBoxLayout()

        file_box = QGroupBox("Files in Bucket")
        fl = QVBoxLayout(file_box)
        self.file_table = QTableWidget()
        self.file_table.setColumnCount(2)
        self.file_table.setHorizontalHeaderLabels(["Key", "Size"])
        self.file_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        fl.addWidget(self.file_table)

        file_btns = QHBoxLayout()
        file_btns.addWidget(QPushButton("Refresh Files", clicked=self._refresh_files))
        file_btns.addWidget(QPushButton("Delete Selected", clicked=self._delete_file))
        fl.addLayout(file_btns)
        r2_mid.addWidget(file_box)

        upload_box = QGroupBox("Upload")
        ul = QVBoxLayout(upload_box)
        ul.addWidget(QLabel("Upload files to selected bucket:"))
        self.upload_prefix = QLineEdit()
        self.upload_prefix.setPlaceholderText("Subfolder prefix (e.g. logos/)")
        ul.addWidget(self.upload_prefix)
        ul.addWidget(QPushButton("Select & Upload Files", clicked=self._upload_files))
        self.upload_progress = QProgressBar()
        ul.addWidget(self.upload_progress)
        self.upload_log = QTextEdit()
        self.upload_log.setReadOnly(True)
        self.upload_log.setMaximumHeight(120)
        self.upload_log.setStyleSheet("font-family:Consolas; font-size:10px")
        ul.addWidget(self.upload_log)
        r2_mid.addWidget(upload_box)

        r2l.addLayout(r2_mid)
        tabs.addTab(r2_tab, "R2 Storage")

        # Worker Tab
        worker_tab = QWidget()
        wl = QVBoxLayout(worker_tab)

        wl.addWidget(QLabel("Deploy Unsubscribe Worker to Cloudflare:"))

        zone_row = QHBoxLayout()
        zone_row.addWidget(QLabel("Zone:"))
        self.zone_cb = QComboBox()
        self.zone_cb.setMinimumWidth(250)
        zone_row.addWidget(self.zone_cb)
        zone_row.addWidget(QPushButton("Refresh Zones", clicked=self._refresh_zones))
        zone_row.addStretch()
        wl.addLayout(zone_row)

        worker_row = QHBoxLayout()
        worker_row.addWidget(QLabel("Worker Name:"))
        self.worker_name = QLineEdit("unsub-worker")
        worker_row.addWidget(self.worker_name)
        worker_row.addWidget(QLabel("KV Namespace:"))
        self.kv_name = QLineEdit("unsubscribes")
        worker_row.addWidget(self.kv_name)
        wl.addLayout(worker_row)

        route_row = QHBoxLayout()
        route_row.addWidget(QLabel("Route Pattern:"))
        self.route_pattern = QLineEdit()
        self.route_pattern.setPlaceholderText("unsub.yourdomain.com/*")
        route_row.addWidget(self.route_pattern)
        wl.addLayout(route_row)

        deploy_btns = QHBoxLayout()
        deploy_btns.addWidget(QPushButton("Deploy Worker", clicked=self._deploy_worker))
        deploy_btns.addWidget(QPushButton("Check Status", clicked=self._check_worker))
        deploy_btns.addStretch()
        wl.addLayout(deploy_btns)

        self.worker_log = QTextEdit()
        self.worker_log.setReadOnly(True)
        self.worker_log.setStyleSheet("font-family:Consolas; background:#1e1e1e; color:#d4d4d4")
        wl.addWidget(self.worker_log)

        tabs.addTab(worker_tab, "Unsubscribe Worker")
        layout.addWidget(tabs)

        self._refresh_accounts()

    def _wlog(self, msg):
        self.worker_log.append(msg)

    def _ulog(self, msg):
        self.upload_log.append(msg)

    def _refresh_accounts(self):
        self.acct_cb.blockSignals(True)
        self.acct_cb.clear()
        for a in self.db.get_cf_accounts():
            self.acct_cb.addItem(a["name"], a["id"])
        self.acct_cb.blockSignals(False)
        if self.acct_cb.count() > 0:
            self.acct_cb.setCurrentIndex(0)

    def _on_account_change(self):
        self._r2 = None
        self.bucket_cb.clear()
        self.cf_domain_list.clear()
        self.status_label.setText("Account changed — click Connect")

    def _add_account(self):
        from PySide6.QtWidgets import QDialog, QFormLayout, QDialogButtonBox, QRadioButton, QButtonGroup
        dlg = QDialog(self)
        dlg.setWindowTitle("Add Cloudflare Account")
        dlg.setMinimumWidth(450)
        fl = QFormLayout(dlg)

        name_in = QLineEdit()
        fl.addRow("Account Name:", name_in)

        auth_group = QButtonGroup(dlg)
        rb_token = QRadioButton("API Token")
        rb_global = QRadioButton("Global API Key")
        rb_token.setChecked(True)
        auth_group.addButton(rb_token)
        auth_group.addButton(rb_global)
        auth_row = QHBoxLayout()
        auth_row.addWidget(rb_token)
        auth_row.addWidget(rb_global)
        fl.addRow("Auth Type:", auth_row)

        token_in = QLineEdit()
        token_in.setEchoMode(QLineEdit.Password)
        token_in.setPlaceholderText("Bearer API Token")
        fl.addRow("API Token:", token_in)

        global_key = QLineEdit()
        global_key.setEchoMode(QLineEdit.Password)
        global_key.setPlaceholderText("Global API Key")
        global_key.setVisible(False)
        fl.addRow("Global Key:", global_key)

        email_in = QLineEdit()
        email_in.setPlaceholderText("Cloudflare login email")
        email_in.setVisible(False)
        fl.addRow("Auth Email:", email_in)

        def toggle_auth():
            is_global = rb_global.isChecked()
            token_in.setVisible(not is_global)
            global_key.setVisible(is_global)
            email_in.setVisible(is_global)

        rb_token.toggled.connect(lambda: toggle_auth())
        rb_global.toggled.connect(lambda: toggle_auth())

        acct_in = QLineEdit()
        acct_in.setPlaceholderText("Account ID")
        fl.addRow("Account ID:", acct_in)

        r2_key = QLineEdit()
        r2_key.setPlaceholderText("R2 Access Key ID (optional)")
        fl.addRow("R2 Access Key:", r2_key)
        r2_sec = QLineEdit()
        r2_sec.setEchoMode(QLineEdit.Password)
        fl.addRow("R2 Secret Key:", r2_sec)

        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        fl.addRow(btns)

        if dlg.exec() == QDialog.Accepted:
            auth_type = "global" if rb_global.isChecked() else "token"
            self.db.add_cf_account(
                name=name_in.text(),
                auth_type=auth_type,
                api_token=token_in.text(),
                global_api_key=global_key.text(),
                auth_email=email_in.text(),
                account_id=acct_in.text(),
                r2_access_key=r2_key.text(),
                r2_secret_key=r2_sec.text(),
            )
            self._refresh_accounts()

    def _delete_account(self):
        cf_id = self.acct_cb.currentData()
        if cf_id and QMessageBox.question(self, "Delete", "Delete this account?") == QMessageBox.Yes:
            self.db.delete_cf_account(cf_id)
            self._refresh_accounts()

    def _get_current_account(self):
        cf_id = self.acct_cb.currentData()
        if not cf_id:
            return None
        row = self.db._conn().execute("SELECT * FROM cf_accounts WHERE id=?", (cf_id,)).fetchone()
        if not row:
            return None
        return dict(row)

    def _get_r2(self):
        if self._r2 and self._r2.enabled:
            return self._r2
        acct = self._get_current_account()
        if not acct:
            return None
        from bulk.mailer.r2_manager import R2Manager
        self._r2 = R2Manager(
            account_id=acct["account_id"],
            api_token=acct["api_token"] or "",
            access_key_id=acct["r2_access_key"] or "",
            secret_access_key=acct["r2_secret_key"] or "",
            global_api_key=acct.get("global_api_key", ""),
            auth_email=acct.get("auth_email", ""),
        )
        return self._r2

    def _connect(self):
        r2 = self._get_r2()
        if r2 and r2.enabled:
            buckets = r2.list_buckets()
            self.status_label.setText(f"Connected — {len(buckets)} buckets")
            self.status_label.setStyleSheet("color:green; font-weight:bold")
            self._refresh_buckets()
        else:
            self.status_label.setText("Connected (no R2 keys)")
            self.status_label.setStyleSheet("color:orange; font-weight:bold")
        self._refresh_zones()
        self.status_label.setText(self.status_label.text() + " — pulling domains...")
        threading.Thread(target=self._pull_domains_bg, daemon=True).start()

    def _pull_domains_bg(self):
        acct = self._get_current_account()
        if not acct:
            return
        import requests
        account_id = acct.get("account_id", "")
        if acct.get("global_api_key") and acct.get("auth_email"):
            headers = {"X-Auth-Key": acct["global_api_key"],
                       "X-Auth-Email": acct["auth_email"]}
        else:
            headers = {"Authorization": f"Bearer {acct.get('api_token', '')}"}

        self.cf_domain_list.clear()
        try:
            resp = requests.get("https://api.cloudflare.com/client/v4/zones",
                                 headers=headers, params={"per_page": 50}, timeout=15)
            zones = resp.json().get("result", []) if resp.status_code == 200 else []
        except Exception:
            zones = []

        brand_domains = {d["domain"] for d in self.db.get_domains()}

        for z in zones:
            name = z["name"]
            in_brands = "✓ Brands" if name in brand_domains else ""

            dom_row = self.db._conn().execute(
                "SELECT unsub_worker_deployed, unsub_domain FROM domains WHERE domain=?", (name,)).fetchone()
            local_unsub = dom_row and dom_row[0] if dom_row else False

            worker_exists = False
            if account_id:
                worker_name = f"unsub-{name.replace('.', '-')}"
                try:
                    wr = requests.get(
                        f"https://api.cloudflare.com/client/v4/accounts/{account_id}/workers/scripts/{worker_name}",
                        headers=headers, timeout=10)
                    worker_exists = wr.status_code == 200
                except Exception:
                    pass

            if worker_exists and not local_unsub and dom_row:
                self.db.mark_unsub_deployed(dom_row["id"] if hasattr(dom_row, "__getitem__") else 0, f"unsub.{name}")

            unsub_str = "✓ Worker" if worker_exists else ("✓ Local" if local_unsub else "")
            self.cf_domain_list.addItem(f"{name}  {in_brands}  {unsub_str}")

    def _refresh_buckets(self):
        r2 = self._get_r2()
        self.bucket_cb.clear()
        for name in r2.list_buckets():
            self.bucket_cb.addItem(name)

    def _create_bucket(self):
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "Create Bucket", "Bucket name:")
        if ok and name.strip():
            r2 = self._get_r2()
            if r2.create_bucket(name.strip()):
                self._refresh_buckets()
                QMessageBox.information(self, "Created", f"Bucket '{name}' created")
            else:
                QMessageBox.warning(self, "Error", "Failed to create bucket")

    def _enable_public(self):
        bucket = self.bucket_cb.currentText()
        if not bucket:
            return
        r2 = self._get_r2()
        if r2.enable_public_access(bucket):
            QMessageBox.information(self, "Public", f"Public access enabled for '{bucket}'")
        else:
            QMessageBox.warning(self, "Error", "Failed — check API token permissions")

    def _add_custom_domain(self):
        bucket = self.bucket_cb.currentText()
        domain = self.custom_domain.text().strip()
        if not bucket or not domain:
            return
        r2 = self._get_r2()
        if r2.add_custom_domain(bucket, domain):
            QMessageBox.information(self, "Domain", f"Custom domain '{domain}' added to '{bucket}'")
        else:
            QMessageBox.warning(self, "Error", "Failed — ensure DNS is configured")

    def _refresh_files(self):
        bucket = self.bucket_cb.currentText()
        if not bucket:
            return
        r2 = self._get_r2()
        objects = r2.list_objects(bucket, self.upload_prefix.text())
        self.file_table.setRowCount(len(objects))
        for i, obj in enumerate(objects):
            self.file_table.setItem(i, 0, QTableWidgetItem(obj["key"]))
            size_kb = obj["size"] / 1024
            self.file_table.setItem(i, 1, QTableWidgetItem(f"{size_kb:.1f} KB"))

    def _delete_file(self):
        bucket = self.bucket_cb.currentText()
        row = self.file_table.currentRow()
        if not bucket or row < 0:
            return
        key = self.file_table.item(row, 0).text()
        if QMessageBox.question(self, "Delete", f"Delete '{key}'?") == QMessageBox.Yes:
            r2 = self._get_r2()
            r2.delete_object(bucket, key)
            self._refresh_files()

    def _upload_files(self):
        bucket = self.bucket_cb.currentText()
        if not bucket:
            QMessageBox.warning(self, "Bucket", "Select a bucket first")
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "Select Files", "",
                                                  "All Files (*)")
        if not paths:
            return

        prefix = self.upload_prefix.text().strip()
        self.upload_progress.setMaximum(len(paths))
        self.upload_progress.setValue(0)
        self.upload_log.clear()

        def upload():
            r2 = self._get_r2()
            for i, path in enumerate(paths):
                name = os.path.basename(path)
                key = f"{prefix}{name}" if prefix else name
                import mimetypes
                ct = mimetypes.guess_type(path)[0] or "application/octet-stream"
                url = r2.upload_file(bucket, key, path, ct)
                if url:
                    self._ulog(f"✓ {key} → {url}")
                else:
                    self._ulog(f"✗ {key} — upload failed")
                self.upload_progress.setValue(i + 1)
            self._ulog(f"\nDone: {len(paths)} files")

        threading.Thread(target=upload, daemon=True).start()

    def _refresh_zones(self):
        r2 = self._get_r2()
        self.zone_cb.clear()
        for zone in r2.list_zones():
            self.zone_cb.addItem(f"{zone['name']} ({zone['id'][:8]}...)", zone["id"])

    def _deploy_worker(self):
        acct = self._get_current_account()
        if not acct:
            QMessageBox.warning(self, "Account", "Select a Cloudflare account")
            return
        account_id = acct["account_id"]
        worker_name = self.worker_name.text().strip()
        kv_name = self.kv_name.text().strip()

        if acct.get("global_api_key") and acct.get("auth_email"):
            headers_auth = {"X-Auth-Key": acct["global_api_key"],
                            "X-Auth-Email": acct["auth_email"],
                            "Content-Type": "application/json"}
            headers_upload = {"X-Auth-Key": acct["global_api_key"],
                              "X-Auth-Email": acct["auth_email"]}
        else:
            api_token = acct.get("api_token", "")
            headers_auth = {"Authorization": f"Bearer {api_token}",
                            "Content-Type": "application/json"}
            headers_upload = {"Authorization": f"Bearer {api_token}"}

        if not all([account_id, worker_name, kv_name]):
            QMessageBox.warning(self, "Missing", "Fill all fields")
            return

        self._wlog("Deploying worker...")

        def deploy():
            import requests

            # Step 1: Create KV namespace
            self._wlog("Creating KV namespace...")
            resp = requests.post(
                f"https://api.cloudflare.com/client/v4/accounts/{account_id}/storage/kv/namespaces",
                headers=headers_auth, json={"title": kv_name}, timeout=30)

            if resp.status_code == 200 and resp.json().get("success"):
                ns_id = resp.json()["result"]["id"]
                self._wlog(f"KV namespace created: {ns_id}")
            elif "already exists" in resp.text.lower():
                # Get existing
                resp2 = requests.get(
                    f"https://api.cloudflare.com/client/v4/accounts/{account_id}/storage/kv/namespaces",
                    headers=headers_auth, timeout=30)
                ns_id = None
                for ns in resp2.json().get("result", []):
                    if ns["title"] == kv_name:
                        ns_id = ns["id"]
                        break
                if ns_id:
                    self._wlog(f"KV namespace exists: {ns_id}")
                else:
                    self._wlog("ERROR: Could not find existing namespace")
                    return
            else:
                self._wlog(f"ERROR: {resp.status_code} {resp.text[:200]}")
                return

            # Step 2: Upload worker script
            self._wlog("Uploading worker script...")
            worker_path = os.path.join(os.path.dirname(__file__), "..", "cloudflare", "unsubscribe-worker.js")
            if not os.path.isfile(worker_path):
                self._wlog(f"ERROR: Worker script not found at {worker_path}")
                return

            with open(worker_path, "r") as f:
                worker_code = f.read()

            metadata = json.dumps({
                "main_module": "worker.mjs",
                "compatibility_date": "2026-04-01",
                "bindings": [{
                    "type": "kv_namespace",
                    "name": "UNSUB_KV",
                    "namespace_id": ns_id,
                }],
            })

            resp = requests.put(
                f"https://api.cloudflare.com/client/v4/accounts/{account_id}/workers/scripts/{worker_name}",
                headers=headers_upload,
                files={
                    "metadata": ("metadata.json", metadata, "application/json"),
                    "worker.mjs": ("worker.mjs", worker_code, "application/javascript+module"),
                }, timeout=30)

            if resp.status_code == 200 and resp.json().get("success"):
                self._wlog("Worker deployed successfully!")
            else:
                self._wlog(f"ERROR: {resp.status_code} {resp.text[:300]}")
                return

            # Step 3: Add route if zone selected
            zone_id = self.zone_cb.currentData()
            route = self.route_pattern.text().strip()
            if zone_id and route:
                self._wlog(f"Adding route: {route}")
                resp = requests.post(
                    f"https://api.cloudflare.com/client/v4/zones/{zone_id}/workers/routes",
                    headers=headers_auth,
                    json={"pattern": route, "script": worker_name},
                    timeout=30)
                if resp.status_code in (200, 201):
                    self._wlog(f"Route added: {route}")
                else:
                    self._wlog(f"Route error: {resp.status_code} {resp.text[:200]}")

            self._wlog("\nDeployment complete!")

        threading.Thread(target=deploy, daemon=True).start()

    def _check_worker(self):
        acct = self._get_current_account()
        if not acct:
            return
        account_id = acct["account_id"]
        worker_name = self.worker_name.text().strip()
        if not all([account_id, worker_name]):
            return

        if acct.get("global_api_key") and acct.get("auth_email"):
            check_headers = {"X-Auth-Key": acct["global_api_key"],
                             "X-Auth-Email": acct["auth_email"]}
        else:
            check_headers = {"Authorization": f"Bearer {acct.get('api_token', '')}"}

        def check():
            import requests
            resp = requests.get(
                f"https://api.cloudflare.com/client/v4/accounts/{account_id}/workers/scripts/{worker_name}",
                headers=check_headers, timeout=15)
            if resp.status_code == 200:
                self._wlog(f"Worker '{worker_name}' is deployed and active")
            else:
                self._wlog(f"Worker '{worker_name}' not found ({resp.status_code})")

        threading.Thread(target=check, daemon=True).start()
