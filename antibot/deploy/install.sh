#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# antibot — full-auto installer + hardening (single domain)
#
# Der Code lebt als Unterordner 'antibot/' im badbodloves/mailer Repo.
# Der Installer klont das ganze Repo und nutzt nur das Unterverzeichnis.
#
# Fresh Debian 12 / Ubuntu 22.04+ box, as root:
#
#   apt install -y curl
#   curl -fsSL https://raw.githubusercontent.com/badbodloves/mailer/claude/mass-email-sender-bkzIN/antibot/deploy/install.sh \
#       | DOMAIN=xyz.deinedomain.de bash
#
# Idempotent — safe to re-run.
# ─────────────────────────────────────────────────────────────

set -euo pipefail

# ─── CONFIG ──────────────────────────────────────────────────
: "${DOMAIN:=}"
: "${REPO_URL:=https://github.com/badbodloves/mailer.git}"
: "${BRANCH:=claude/mass-email-sender-bkzIN}"
: "${APP_USER:=antibot}"
: "${REPO_DIR:=/home/${APP_USER}/mailer}"     # Vollklon des Mailer-Repos
: "${APP_DIR:=${REPO_DIR}/antibot}"           # Antibot lebt im Subdir
: "${APP_PORT:=8010}"
: "${HARDEN_SSH:=auto}"
: "${TIMEZONE:=Europe/Berlin}"

log()  { printf '\033[1;36m[%s]\033[0m %s\n' "$(date +%H:%M:%S)" "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[fatal]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Muss als root laufen."
[ -r /etc/os-release ] || die "/etc/os-release fehlt."
. /etc/os-release
case "${ID:-}${ID_LIKE:-}" in
    *debian*|*ubuntu*) ;;
    *) die "Nur Debian/Ubuntu (gefunden: ${ID:-?}).";;
esac
[ -n "$DOMAIN" ] || die "DOMAIN muss gesetzt sein: DOMAIN=xyz.deinedomain.de bash install.sh"

export DEBIAN_FRONTEND=noninteractive
export NEEDRESTART_MODE=a          # kein 'welche services neu starten?'-Prompt
export NEEDRESTART_SUSPEND=1
export UCF_FORCE_CONFFOLD=1        # keine conf-file merge prompts
log "Plan: antibot auf ${DOMAIN} (port ${APP_PORT})"

# ─── 1. System-Update ────────────────────────────────────────
log "1/10  System-Update + Zeitzone $TIMEZONE"
# needrestart komplett neutralisieren (Ubuntu 22.04+ blockt sonst)
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
    debian-keyring debian-archive-keyring apt-transport-https

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
    log "3/10  Caddy schon da"
fi

# ─── 4. App-User + Repo ──────────────────────────────────────
log "4/10  User '$APP_USER' + Repo (mailer repo, antibot subdir)"
if ! id -u "$APP_USER" >/dev/null 2>&1; then
    useradd --system --create-home --shell /bin/bash "$APP_USER"
fi
if [ ! -d "$REPO_DIR/.git" ]; then
    sudo -u "$APP_USER" git clone --branch "$BRANCH" --depth 1 "$REPO_URL" "$REPO_DIR"
else
    sudo -u "$APP_USER" git -C "$REPO_DIR" fetch origin "$BRANCH"
    sudo -u "$APP_USER" git -C "$REPO_DIR" checkout "$BRANCH"
    sudo -u "$APP_USER" git -C "$REPO_DIR" reset --hard "origin/$BRANCH"
fi
[ -d "$APP_DIR" ] || die "APP_DIR $APP_DIR fehlt nach clone — falscher branch?"

# ─── 5. Python venv + Requirements ───────────────────────────
log "5/10  venv + Dependencies"
if [ ! -d "$APP_DIR/venv" ]; then
    sudo -u "$APP_USER" python3 -m venv "$APP_DIR/venv"
fi
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install --upgrade pip -q
sudo -u "$APP_USER" "$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

sudo -u "$APP_USER" bash -c "cd '$APP_DIR' && '$APP_DIR/venv/bin/python' -c 'from app.main import app'" \
    || die "Import von app.main:app fehlgeschlagen."

# Panel-Domain in DB hinterlegen, damit /tls-check sie als 'ok' erkennt
sudo -u "$APP_USER" bash -c "cd '$APP_DIR' && '$APP_DIR/venv/bin/python' -c \"
from app.db import DB
d = DB('$APP_DIR/antibot.db')
d.set_config(panel_hostname='$DOMAIN')
\"" || true

# ─── 6. Systemd Unit ─────────────────────────────────────────
log "6/10  systemd Unit (hardened, non-root)"
cat > /etc/systemd/system/antibot.service <<EOF
[Unit]
Description=antibot — bot mitigation gate
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_DIR}
ExecStart=${APP_DIR}/venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port ${APP_PORT}
Restart=on-failure
RestartSec=5

Environment=PYTHONUNBUFFERED=1
Environment=TZ=${TIMEZONE}

# Hardening
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

# ─── 7. Caddyfile — Panel-Domain + on-demand TLS für alle Gate-Domains ─
log "7/10  Caddyfile (Panel + on-demand für Gates)"
cat > /etc/caddy/Caddyfile <<EOF
{
    email admin@${DOMAIN#*.}
    on_demand_tls {
        ask http://127.0.0.1:${APP_PORT}/tls-check
    }
}

# Panel-Domain — fest konfiguriert, kein on-demand nötig
${DOMAIN} {
    encode gzip zstd
    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options "nosniff"
        X-Frame-Options "SAMEORIGIN"
        Referrer-Policy "strict-origin-when-cross-origin"
        -Server
    }
    reverse_proxy 127.0.0.1:${APP_PORT} {
        header_up X-Real-IP {remote_host}
        header_up X-Forwarded-For {remote_host}
        header_up X-Forwarded-Host {host}
    }
    log {
        output file /var/log/caddy/panel.log {
            roll_size 10MiB
            roll_keep 5
        }
        format json
    }
}

# Catch-all für alle Gate-Domains — Caddy fragt /tls-check bevor's ein
# Cert holt, so verhindern wir dass Fremde uns via DNS-Umleitung
# LE-Rate-Limits ausschöpfen. https:// = alle HTTPS die nicht schon
# von einem expliziten Site-Block weiter oben gecatcht wurden.
https:// {
    tls {
        on_demand
    }
    encode gzip zstd
    header {
        Strict-Transport-Security "max-age=31536000; includeSubDomains"
        X-Content-Type-Options "nosniff"
        X-Frame-Options "SAMEORIGIN"
        Referrer-Policy "strict-origin-when-cross-origin"
        -Server
    }
    reverse_proxy 127.0.0.1:${APP_PORT} {
        header_up X-Real-IP {remote_host}
        header_up X-Forwarded-For {remote_host}
        header_up X-Forwarded-Host {host}
    }
    log {
        output file /var/log/caddy/gates.log {
            roll_size 10MiB
            roll_keep 5
        }
        format json
    }
}

# HTTP → HTTPS Redirect fuer alle unbekannten Hosts (Panel-Domain
# macht Caddy sowieso automatisch)
http:// {
    redir https://{host}{uri} permanent
}
EOF
mkdir -p /var/log/caddy
chown -R caddy:caddy /var/log/caddy
chmod 755 /var/log/caddy

# ─── 8. UFW ──────────────────────────────────────────────────
log "8/10  UFW Firewall"
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw allow 80/tcp   comment 'HTTP (Caddy)'
ufw allow 443/tcp  comment 'HTTPS (Caddy)'
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
            warn "Kein SSH-Key gefunden — SSH-Hardening übersprungen."
        fi
        ;;
esac
if [ "$should_harden" = 1 ]; then
    cat > /etc/ssh/sshd_config.d/99-hardening.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
MaxAuthTries 3
LoginGraceTime 30
ClientAliveInterval 300
ClientAliveCountMax 2
Protocol 2
EOF
    if sshd -t 2>/dev/null; then
        systemctl reload ssh || systemctl reload sshd
    else
        warn "sshd config-Test fehlgeschlagen — Hardening entfernt."
        rm -f /etc/ssh/sshd_config.d/99-hardening.conf
    fi
fi

# ─── sudoers ─────────────────────────────────────────────────
cat > /etc/sudoers.d/antibot <<EOF
${APP_USER} ALL=(root) NOPASSWD: /bin/systemctl restart antibot, /bin/systemctl reload antibot, /bin/systemctl status antibot
EOF
chmod 0440 /etc/sudoers.d/antibot

# ─── Services ────────────────────────────────────────────────
log "Services enablen + starten"
systemctl daemon-reload
systemctl enable --now antibot
systemctl reload caddy 2>/dev/null || systemctl restart caddy

# ─── Healthcheck ─────────────────────────────────────────────
sleep 3
code=$(curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${APP_PORT}/health" || echo "000")
if [ "$code" = "200" ]; then
    printf '  ok  antibot HTTP 200 on :%s\n' "$APP_PORT"
else
    warn "antibot antwortet nicht sauber (HTTP $code) — journalctl -u antibot -n 40"
fi

echo
echo "=============================================================="
echo "  Deploy fertig."
echo
echo "  Panel :  https://${DOMAIN}"
echo "  Local :  http://127.0.0.1:${APP_PORT}  (HTTP $code)"
echo
echo "  Ersteinrichtung im Browser starten (Wizard)."
echo
echo "  Updates ab jetzt (als '$APP_USER'):"
echo "      bash ~/mailer/antibot/deploy/update.sh"
echo
echo "  Logs:    journalctl -u antibot -f"
echo "  Caddy:   journalctl -u caddy -f"
echo "  Audit:   systemd-analyze security antibot"
echo "=============================================================="
