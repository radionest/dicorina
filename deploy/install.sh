#!/usr/bin/env bash
set -euo pipefail
DEST="${DEST:-/opt/dicorina}"
PYTHON="${PYTHON:-python3.12}"

command -v "$PYTHON" >/dev/null 2>&1 || { echo "error: '$PYTHON' not found (set PYTHON= to override)" >&2; exit 1; }
"$PYTHON" -c 'import ensurepip, venv' 2>/dev/null || { echo "error: '$PYTHON' lacks venv/ensurepip support; install the venv package for '$PYTHON'" >&2; exit 1; }

mkdir -p "$DEST" /etc/dicorina
rm -rf "$DEST/src"
cp -r src pyproject.toml deploy/requirements.txt "$DEST"/
[ -f /etc/dicorina/config.toml ] || cp deploy/config.example.toml /etc/dicorina/config.toml

cd "$DEST"
"$PYTHON" -m venv .venv
.venv/bin/pip install --require-hashes -r requirements.txt
.venv/bin/pip install --no-deps .
id -u dicorina &>/dev/null || useradd --system --no-create-home --shell /usr/sbin/nologin dicorina
install -d -o dicorina -g dicorina /var/cache/dicorina
chown -R dicorina:dicorina "$DEST"
install -m 0644 "$OLDPWD/deploy/dicorina.service" /etc/systemd/system/dicorina.service
systemctl daemon-reload
echo "Installed. Edit /etc/dicorina/config.toml, then: systemctl enable --now dicorina"
