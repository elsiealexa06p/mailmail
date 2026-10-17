#!/usr/bin/env bash
# Regulärer Deploy: git pull + venv sync + restart der installierten Services.
# Als App-User laufen lassen — sudoers erlaubt die restarts ohne PW.
#
#   bash ~/mailer/deploy/update.sh

set -euo pipefail

: "${APP_DIR:=$HOME/mailer}"
: "${BRANCH:=claude/mass-email-sender-bkzIN}"

cd "$APP_DIR"

echo "→ git pull ($BRANCH)"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git reset --hard "origin/$BRANCH"

if [ -x venv/bin/pip ]; then
    echo "→ pip install -r requirements.txt (falls neu)"
    venv/bin/pip install -q --upgrade pip
    venv/bin/pip install -q -r requirements.txt
fi

# Restart nur die Services, die auch tatsächlich installiert sind.
for svc in transmailer bulkmailer; do
    if systemctl list-unit-files | grep -q "^${svc}.service"; then
        echo "→ systemctl restart $svc"
        sudo -n systemctl restart "$svc"
    fi
done

sleep 2
for svc in transmailer bulkmailer; do
    if systemctl list-unit-files | grep -q "^${svc}.service"; then
        echo
        sudo -n systemctl status "$svc" --no-pager | head -6
    fi
done

echo
echo "✅ Deploy fertig."
