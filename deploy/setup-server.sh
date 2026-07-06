#!/usr/bin/env bash
# One-time setup on Vultr (Ubuntu). Run as root or with sudo.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/recon}"
REPO_URL="${REPO_URL:-}"

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo: sudo bash deploy/setup-server.sh"
  exit 1
fi

apt-get update
apt-get install -y git nginx certbot python3-certbot-nginx curl

if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi

if ! docker compose version >/dev/null 2>&1; then
  apt-get install -y docker-compose-plugin
fi

mkdir -p "$APP_DIR"
if [[ -n "$REPO_URL" && ! -d "$APP_DIR/.git" ]]; then
  git clone "$REPO_URL" "$APP_DIR"
fi

if [[ ! -f "$APP_DIR/server/.env" ]]; then
  cp "$APP_DIR/server/.env.example" "$APP_DIR/server/.env"
  echo ""
  echo ">>> Edit $APP_DIR/server/.env with production secrets, then run:"
  echo "    cd $APP_DIR && docker compose up -d --build"
  echo ""
fi

if [[ -f "$APP_DIR/deploy/nginx/recon.vaidikedu.com.conf" ]]; then
  cp "$APP_DIR/deploy/nginx/recon.vaidikedu.com.conf" /etc/nginx/sites-available/recon
  ln -sf /etc/nginx/sites-available/recon /etc/nginx/sites-enabled/recon
  rm -f /etc/nginx/sites-enabled/default
  nginx -t && systemctl reload nginx
fi

ufw allow OpenSSH || true
ufw allow 'Nginx Full' || true
ufw --force enable || true

echo "Done. Next:"
echo "  1. Set server/.env (DASHBOARD_BASE_URL=https://recon.vaidikedu.com)"
echo "  2. certbot --nginx -d recon.vaidikedu.com"
echo "  3. cd $APP_DIR && docker compose up -d --build"
