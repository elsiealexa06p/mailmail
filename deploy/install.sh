#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# Mailer — full-auto installer + hardening (trans + bulk)
#
# Runs on a fresh Debian 12 / Ubuntu 22.04+ box, as root.
# End state: hardened non-root systemd services reachable via
# Caddy-terminated HTTPS on the domains you set, protected by
# UFW + fail2ban with automatic security updates. Idempotent.
#
# Usage:
#   apt install -y curl
#   curl -fsSL https://raw.githubusercontent.com/badbodloves/mailer/claude/mass-email-sender-bkzIN/deploy/install.sh \
#       | DOMAIN_TRANS=trans.deinedomain.de DOMAIN_BULK=bulk.deinedomain.de bash
#
# Nur eine der beiden Domains setzen → nur dieses Panel wird installiert.
# ─────────────────────────────────────────────────────────────

set -euo pipefail

# ─── CONFIG ──────────────────────────────────────────────────
: "${DOMAIN_TRANS:=}"                                             # leer = trans nicht installieren
: "${DOMAIN_BULK:=}"                                              # leer = bulk nicht installieren
: "${REPO_URL:=https://github.com/badbodloves/mailer.git}"
: "${BRANCH:=claude/mass-email-sender-bkzIN}"
: "${APP_USER:=mailer}"
: "${APP_DIR:=/home/${APP_USER}/mailer}"
: "${TRANS_PORT:=8001}"
: "${BULK_PORT:=8000}"
: "${HARDEN_SSH:=auto}"                                           # auto | yes | no
: "${TIMEZONE:=Europe/Berlin}"

# ─── HELPERS ─────────────────────────────────────────────────
log()  { printf '\033[1;36m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[fatal]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Muss als root laufen."
[ -r /etc/os-release ] || die "/etc/os-release fehlt — unbekanntes OS?"
. /etc/os-release
case "${ID:-}${ID_LIKE:-}" in
    *debian*|*ubuntu*) ;;
    *) die "Nur Debian/Ubuntu unterstützt (gefunden: ${ID:-?}).";;
esac

INSTALL_TRANS=0; INSTALL_BULK=0
[ -n "$DOMAIN_TRANS" ] && INSTALL_TRANS=1
[ -n "$DOMAIN_BULK" ]  && INSTALL_BULK=1
if [ $INSTALL_TRANS -eq 0 ] && [ $INSTALL_BULK -eq 0 ]; then
    die "Setze mindestens DOMAIN_TRANS oder DOMAIN_BULK (oder beide)."
fi

export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a          # kein 'welche services neu starten?'-Prompt
export NEEDRESTART_SUSPEND=1
export UCF_FORCE_CONFFOLD=1
plan=""; [ $INSTALL_TRANS -eq 1 ] && plan+="trans (${DOMAIN_TRANS}:${TRANS_PORT}) "
[ $INSTALL_BULK -eq 1 ]  && plan+="bulk (${DOMAIN_BULK}:${BULK_PORT})"
log "Plan: $plan"

# ─── 1. System-Update + Zeitzone ─────────────────────────────
log "1/10  System-Update + Zeitzone $TIMEZONE"
if [ -f /etc/needrestart/needrestart.conf ]; then
    sed -i "s/^#\?\$nrconf{restart} = .*/\$nrconf{restart} = 'a';/" /etc/needrestart/needrestart.conf
    sed -i "s/^#\?\$nrconf{kernelhints} = .*/\$nrconf{kernelhints} = 0;/" /etc/needrestart/needrestart.conf
fi
apt-get update -qq
apt-get -yqq \
    -o Dpkg::Options::="--force-confdef" \
    -o Dpkg::Options::="--force-confold" \
    -o APT::Get::Assume-Yes=true \
    upgrade
timedatectl set-timezone "$TIMEZONE" || true

# ─── 2. Pakete ───────────────────────────────────────────────
log "2/10  Pakete installieren"
apt-get install -yqq \
    git curl ca-certificates gnupg \
    python3 python3-venv python3-pip \
    ufw fail2ban unattended-upgrades apt-listchanges \
    debian-keyring debian-archive-keyring apt-transport-https \
    qpdf libjpeg-turbo8-dev zlib1g-dev

# ─── 3. Caddy ────────────────────────────────────────────────
if ! command -v caddy >/dev/null; then
    log "3/10  Caddy installieren"
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
        > /etc/apt/sources.list.d/caddy-stable.list
    apt-get update -qq
    apt-get install -yqq caddy
else
    log "3/10  Caddy schon installiert — skip"
fi

# ─── 4. App-User + Repo ──────────────────────────────────────
log "4/10  User '$APP_USER' + Repo"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
    useradd --system --create-home --shell /bin/bash "$APP_USER"
fi
if [ ! -d "$APP_DIR/.git" ]; then
    sudo -u "$APP_USER" git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
else
    log "     Repo existiert — fetch + reset auf $BRANCH"
    sudo -u "$APP_USER" git -C "$APP_DIR" fetch origin "$BRANCH"
    sudo -u "$APP_USER" git -C "$APP_DIR" checkout "$BRANCH"
    sudo -u "$APP_USER" git -C "$APP_DIR" reset --hard "origin/$BRANCH"
fi

# ─── 5. Python venv + Requirements ───────────────────────────
log "5/10  Python venv + Dependencies"
if [ ! -d "$APP_DIR/venv" ]; then
    sudo -u "$APP_USER" python3 -m venv "$APP_DIR/venv"
fi
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --upgrade pip -q
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
# bulk/requirements.txt zieht PySide6 rein (GUI, unnötig fürs Web-Panel)
# — die restlichen Web-Deps holen wir explizit
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install -q \
    fastapi 'uvicorn[standard]' jinja2 python-multipart boto3 pypdf

# Import-Smoke-Tests für alles was installiert wird
if [ $INSTALL_TRANS -eq 1 ]; then
    sudo -u "$APP_USER" bash -c "cd '$APP_DIR' && '$APP_DIR/venv/bin/python' -c 'from transactional.web.main import app'" \
        || die "transactional.web.main:app import fehlgeschlagen."
fi
if [ $INSTALL_BULK -eq 1 ]; then
    sudo -u "$APP_USER" bash -c "cd '$APP_DIR' && '$APP_DIR/venv/bin/python' -c 'from bulk.web.main import app'" \
        || die "bulk.web.main:app import fehlgeschlagen."
fi

# ─── 6. Systemd Units (gehärtet, non-root) ───────────────────
log "6/10  systemd Units"

write_unit() {
    local name="$1" module="$2" port="$3"
    cat > "/etc/systemd/system/${name}.service" <<EOF
[Unit]
Description=${name} Web UI
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/venv/bin/python -m uvicorn ${module}:app --host 127.0.0.1 --port ${port}
Restart=on-failure
RestartSec=5

Environment=PYTHONUNBUFFERED=1
Environment=TZ=${TIMEZONE}

# ── Hardening ──
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=${APP_DIR}
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
ProtectProc=invisible
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
SystemCallArchitectures=native
CapabilityBoundingSet=
AmbientCapabilities=
UMask=0077

[Install]
WantedBy=multi-user.target
EOF
}

[ $INSTALL_TRANS -eq 1 ] && write_unit transmailer transactional.web.main "$TRANS_PORT"
[ $INSTALL_BULK -eq 1 ]  && write_unit bulkmailer  bulk.web.main          "$BULK_PORT"

# ─── 7. Caddyfile ───────────────────────────────────────────
log "7/10  Caddyfile"
# Global options block als Multiline — Caddy stolpert bei manchen
# Versionen ueber die Einzeiler-Form.
{
    if [ $INSTALL_TRANS -eq 1 ]; then
        cat <<GLOBAL
{
    email admin@${DOMAIN_TRANS#*.}
}

GLOBAL
    else
        cat <<GLOBAL
{
    email admin@${DOMAIN_BULK#*.}
}

GLOBAL
    fi

    write_vhost() {
        local domain="$1" port="$2"
        cat <<VHOST
${domain} {
    encode gzip zstd
    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options    "nosniff"
        X-Frame-Options           "SAMEORIGIN"
        Referrer-Policy           "strict-origin-when-cross-origin"
        Permissions-Policy        "geolocation=(), microphone=(), camera=()"
        -Server
    }
    reverse_proxy 127.0.0.1:${port} {
        header_up X-Real-IP        {remote_host}
        header_up X-Forwarded-For  {remote_host}
        header_up X-Forwarded-Host {host}
    }
    log {
        output file /var/log/caddy/${domain}.log {
            roll_size 10MiB
            roll_keep 5
        }
        format json
    }
}

VHOST
    }

    [ $INSTALL_TRANS -eq 1 ] && write_vhost "$DOMAIN_TRANS" "$TRANS_PORT"
    [ $INSTALL_BULK -eq 1 ]  && write_vhost "$DOMAIN_BULK"  "$BULK_PORT"
} > /etc/caddy/Caddyfile

mkdir -p /var/log/caddy && chown caddy:caddy /var/log/caddy

# ─── 8. UFW Firewall ─────────────────────────────────────────
# WICHTIG: SSH-Regel MUSS vor 'ufw enable' stehen, sonst sperrt sich
# UFW beim Aktivieren selber aus. Manche Cloud-Kernel droppen zusaetzlich
# bestehende SSH-Sessions beim iptables-flush — falls dir das passiert,
# einfach reconnecten und dieses Script nochmal starten (idempotent).
log "8/10  UFW Firewall (nur 22 / 80 / 443)"
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 80/tcp   comment 'HTTP (Caddy)'
ufw allow 443/tcp  comment 'HTTPS (Caddy)'
# 'ufw --force enable' bypasst den y/n-Prompt sauber. 'yes | ufw enable'
# broch unter 'set -euo pipefail' weil 'yes' SIGPIPE bekommt (exit 141)
# sobald ufw stdin schließt, pipefail macht Fehler draus, set -e killt.
ufw --force enable >/dev/null

# ─── 9. fail2ban + unattended-upgrades ──────────────────────
log "9/10  fail2ban + unattended-upgrades"
cat > /etc/fail2ban/jail.d/sshd.conf <<'EOF'
[sshd]
enabled  = true
port     = ssh
backend  = systemd
maxretry = 5
findtime = 10m
bantime  = 1h
EOF
systemctl enable --now fail2ban
systemctl restart fail2ban

cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade   "1";
APT::Periodic::AutocleanInterval    "7";
EOF
cat > /etc/apt/apt.conf.d/50unattended-upgrades <<'EOF'
Unattended-Upgrade::Origins-Pattern {
    "origin=Debian,codename=${distro_codename},label=Debian-Security";
    "origin=Ubuntu,codename=${distro_codename}-security";
};
Unattended-Upgrade::Automatic-Reboot "false";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
EOF
systemctl enable --now unattended-upgrades

# ─── 10. SSH-Hardening ──────────────────────────────────────
log "10/10 SSH-Hardening ($HARDEN_SSH)"
should_harden=0
case "$HARDEN_SSH" in
    yes) should_harden=1 ;;
    no)  should_harden=0 ;;
    auto)
        if [ -s /root/.ssh/authorized_keys ] \
           || ([ -d "/home/$APP_USER/.ssh" ] && [ -s "/home/$APP_USER/.ssh/authorized_keys" ]); then
            should_harden=1
        else
            warn "Kein SSH-Key gefunden — SSH-Hardening übersprungen, sonst sperrst du dich aus."
            warn "Später mit HARDEN_SSH=yes nachziehen wenn du einen Key hinterlegt hast."
        fi
        ;;
esac
if [ "$should_harden" = 1 ]; then
    cat > /etc/ssh/sshd_config.d/99-hardening.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no
PermitRootLogin prohibit-password
X11Forwarding no
MaxAuthTries 3
LoginGraceTime 30
ClientAliveInterval 300
ClientAliveCountMax 2
Protocol 2
EOF
    if sshd -t 2>/dev/null; then
        systemctl reload ssh || systemctl reload sshd
        log "     SSH: Passwort-Auth aus, nur noch Key-Login."
    else
        warn "sshd config-Test fehlgeschlagen — Hardening-Datei entfernt."
        rm -f /etc/ssh/sshd_config.d/99-hardening.conf
    fi
fi

# ─── sudoers: NOPASSWD restart für installierte Services ────
sudo_cmds=()
[ $INSTALL_TRANS -eq 1 ] && sudo_cmds+=("/bin/systemctl restart transmailer" "/bin/systemctl reload transmailer" "/bin/systemctl status transmailer")
[ $INSTALL_BULK -eq 1 ]  && sudo_cmds+=("/bin/systemctl restart bulkmailer"  "/bin/systemctl reload bulkmailer"  "/bin/systemctl status bulkmailer")
IFS=, ; cmd_list="${sudo_cmds[*]}" ; unset IFS
cat > /etc/sudoers.d/mailer <<EOF
${APP_USER} ALL=(root) NOPASSWD: ${cmd_list}
EOF
chmod 0440 /etc/sudoers.d/mailer

# ─── Services scharfschalten ─────────────────────────────────
log "Services enablen + starten"
systemctl daemon-reload
[ $INSTALL_TRANS -eq 1 ] && systemctl enable --now transmailer
[ $INSTALL_BULK -eq 1 ]  && systemctl enable --now bulkmailer
systemctl reload caddy 2>/dev/null || systemctl restart caddy

# ─── Healthchecks ────────────────────────────────────────────
sleep 3
check() {
    local name="$1" port="$2"
    local code
    code=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${port}/" || echo "000")
    if [ "$code" = "303" ] || [ "$code" = "200" ] || [ "$code" = "307" ]; then
        printf '  ✓ %-10s HTTP %s on :%s\n' "$name" "$code" "$port"
    else
        warn "$name antwortet nicht sauber (HTTP $code) — journalctl -u ${name} -n 40"
    fi
}
[ $INSTALL_TRANS -eq 1 ] && check transmailer "$TRANS_PORT"
[ $INSTALL_BULK -eq 1 ]  && check bulkmailer  "$BULK_PORT"

# ─── Abschluss (ASCII-only damit LANG=C-Server nicht meckern) ─
echo
echo "=============================================================="
echo "  Deploy fertig."
echo
[ $INSTALL_TRANS -eq 1 ] && echo "  Trans :  https://${DOMAIN_TRANS}"
[ $INSTALL_BULK -eq 1 ]  && echo "  Bulk  :  https://${DOMAIN_BULK}"
echo
echo "  DNS-Check: die Domains oben muessen bereits auf diesen"
echo "  Server zeigen (A-/AAAA-Record). Beim ersten Aufruf"
echo "  holt Caddy Let's Encrypt automatisch."
echo
echo "  Updates ab jetzt (in SSH-Session als '$APP_USER'):"
echo "      bash ~/mailer/deploy/update.sh"
echo
echo "  Logs:    journalctl -u transmailer -f   (oder bulkmailer)"
echo "  Caddy:   journalctl -u caddy -f"
echo "  Audit:   systemd-analyze security transmailer"
echo "=============================================================="
