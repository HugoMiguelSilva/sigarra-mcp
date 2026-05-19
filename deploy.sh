#!/usr/bin/env bash
set -euo pipefail

# deploy.sh — safe deploy script for the sigarra-mcp project
# Usage:
#   ./deploy.sh [branch]
# Example:
#   ./deploy.sh site

BRANCH=${1:-site}
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Deploying branch '$BRANCH' from $DIR"
cd "$DIR"

echo "Fetching from origin..."
git fetch origin --prune

echo "Checking out $BRANCH..."
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

if [ ! -d ".venv" ]; then
  echo "Creating virtual environment (.venv)"
  python3 -m venv .venv
fi

echo "Activating virtualenv"
# shellcheck source=/dev/null
source .venv/bin/activate

echo "Upgrading pip and installing requirements"
pip install --upgrade pip
pip install -r requirements.txt

echo "Restarting systemd service sigarra-web"
sudo systemctl restart sigarra-web

echo "Waiting briefly for service to come up..."
sleep 1
sudo systemctl status --no-pager sigarra-web

echo "Deploy finished."
