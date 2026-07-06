#!/bin/bash
# Shared proxy provisioning (e2e run.sh + bench.sh): install dicorina via the REAL
# deploy/install.sh + systemd, start the service with the given config, wait for HTTP
# health. Exit 0 (+ touch $BARRIER/ready_proxy) when healthy, 1 otherwise.
set -x
R=/repo/staging/.data/vm-net
BARRIER="$R/barrier"
mkdir -p "$R" "$BARRIER"
CONFIG="${1:?usage: proxy_install.sh <config.toml>}"

# cloud-init's runcmd runs role.sh with HOME unset; uv installs to /root/.local/bin, but
# "$HOME/.local/bin" then expands to "/.local/bin", leaving uv off this script's PATH and
# breaking the `uv python` calls below. Pin HOME for root.
export HOME=/root
# deploy/install.sh hard-requires python3.12 (PYTHON= to override); buster ships no
# python3.12, so install a uv-managed 3.12 and hand it to install.sh via PYTHON=.
export PATH="$HOME/.local/bin:$PATH"
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"

# install.sh uses relative paths (src/, pyproject.toml, deploy/config.example.toml,
# deploy/dicorina.service via $OLDPWD) — must be invoked with CWD = repo root.
# uv fetches a managed Python 3.12; place it in a world-readable dir so the dicorina service
# user (User=dicorina) can read the interpreter the venv links to (default /root/.local/share/uv
# is mode 700). NOTE: not host-verifiable — confirm at the live run.
export UV_PYTHON_INSTALL_DIR=/opt/uv/python
install -d -m 0755 /opt/uv
uv python install 3.12
# --no-project: /repo/.venv is the HOST's venv (broken symlinks over 9p) — project
# discovery would fail and leave PYTHON empty, falling back to the ~/.local/bin shim;
# a venv linked through /root is unreadable for User=dicorina (203/EXEC crash-loop).
PY312="$(uv python find --no-project 3.12)"
[ -n "$PY312" ] || { echo "FATAL: uv python find returned nothing" >&2; exit 1; }
(cd /repo && DEST=/opt/dicorina PYTHON="$PY312" bash /repo/deploy/install.sh)
chmod -R o+rX /opt/uv 2>/dev/null || true
install -m 0644 "$CONFIG" /etc/dicorina/config.toml
systemctl enable --now dicorina

for _ in $(seq 1 120); do
  if curl -fsS http://localhost:8042/health >/dev/null 2>&1; then
    curl -s http://localhost:8042/health > "$R/proxy-health.json" || true
    touch "$BARRIER/ready_proxy"
    exit 0
  fi
  sleep 5
done
curl -s http://localhost:8042/health > "$R/proxy-health.json" || true
{ systemctl status dicorina --no-pager; journalctl -u dicorina -n 80 --no-pager; } > "$R/proxy-journal.txt" 2>&1 || true
exit 1
