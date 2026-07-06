#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/recon}"
cd "$APP_DIR"

git fetch origin main
git reset --hard origin/main

docker compose build --pull
docker compose up -d
docker image prune -f

curl -fsS http://127.0.0.1:4050/health
echo ""
echo "Deploy OK — https://recon.vaidikedu.com/health"
