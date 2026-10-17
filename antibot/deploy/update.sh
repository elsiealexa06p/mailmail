#!/usr/bin/env bash
# Regulärer Deploy: git pull im mailer-Repo + venv sync + restart.
# Als antibot-User laufen lassen — sudoers erlaubt den restart ohne PW.
#
#   bash ~/mailer/antibot/deploy/update.sh

set -euo pipefail

: "${REPO_DIR:=$HOME/mailer}"                      # Vollklon des mailer-Repos
: "${APP_DIR:=$REPO_DIR/antibot}"                  # Antibot Subdir
: "${BRANCH:=claude/mass-email-sender-bkzIN}"

cd "$REPO_DIR"

echo "→ git pull ($BRANCH) — mailer repo"
git fetch origin "$BRANCH"
git checkout "$BRANCH"
git reset --hard "origin/$BRANCH"

if [ -x "$APP_DIR/venv/bin/pip" ]; then
    echo "→ pip install -r antibot/requirements.txt (falls neu)"
    "$APP_DIR/venv/bin/pip" install -q --upgrade pip
    "$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
fi

echo "→ systemctl restart antibot"
sudo -n systemctl restart antibot

sleep 2
sudo -n systemctl status antibot --no-pager | head -8
echo "✅ Deploy fertig."
