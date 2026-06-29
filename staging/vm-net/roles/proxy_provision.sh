#!/bin/bash
# Runs inside the proxy VM. Installs dicorina via the REAL deploy/install.sh + systemd
# (no Orthanc), starts the service from the stand config, waits for both clients, then
# probes eviction and records proxy.json.
set -x
R=/repo/staging/.data/vm-net
BARRIER="$R/barrier"
mkdir -p "$R" "$BARRIER"
exec > >(tee -a "$R/proxy-provision.log" /dev/ttyS0) 2>&1

# cloud-init's runcmd runs role.sh with HOME unset; uv installs to /root/.local/bin, but
# "$HOME/.local/bin" then expands to "/.local/bin", leaving uv off install.sh's PATH and
# aborting `uv sync` (set -e) before the service file is laid down. Pin HOME for root.
export HOME=/root
# uv brings its own managed Python 3.12 (proxy base image Python version is irrelevant).
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
(cd /repo && DEST=/opt/dicorina bash /repo/deploy/install.sh)
chmod -R o+rX /opt/uv 2>/dev/null || true
install -m 0644 /repo/staging/vm-net/config/proxy.toml /etc/dicorina/config.toml
systemctl enable --now dicorina

# wait for HTTP readiness
HEALTHY=false
for _ in $(seq 1 120); do
  if curl -fsS http://localhost:8042/health >/dev/null 2>&1; then HEALTHY=true; break; fi
  sleep 5
done
curl -s http://localhost:8042/health > "$R/proxy-health.json" || true
if [ "$HEALTHY" = true ]; then
  touch "$BARRIER/ready_proxy"
else
  python3 - "$R/proxy.json" <<'PY'
import json, sys
json.dump({"role": "proxy", "studies_before_evict": 0, "studies_after_evict": 0},
          open(sys.argv[1], "w", encoding="utf-8"), ensure_ascii=False)
PY
  touch "$BARRIER/ready_proxy_done"
  touch "$R/proxy-done"
  exit 1
fi

# wait until both clients signalled completion (max ~30 min)
for _ in $(seq 1 360); do
  [ -f "$BARRIER/ready_clienta_done" ] && [ -f "$BARRIER/ready_clientb_done" ] && break
  sleep 5
done

count_studies() { find /var/cache/dicorina -mindepth 1 -maxdepth 1 -type d 2>/dev/null | wc -l; }
BEFORE=$(count_studies)

# S7: short TTL + tiny cap + short interval, then wait one interval so the in-process loop evicts.
systemctl stop dicorina
python3 - <<'PY'
import re, pathlib
p = pathlib.Path("/etc/dicorina/config.toml"); t = p.read_text()
t = re.sub(r"disk_ttl_hours = .*", "disk_ttl_hours = 0", t)
t = re.sub(r"disk_max_size_gb = .*", "disk_max_size_gb = 0.00001", t)
t = re.sub(r"eviction_interval_seconds = .*", "eviction_interval_seconds = 5.0", t)
p.write_text(t)
PY
systemctl start dicorina
sleep 20
AFTER=$(count_studies)

python3 - "$R/proxy.json" "$BEFORE" "$AFTER" <<'PY'
import json, sys
path, before, after = sys.argv[1:4]
json.dump({"role": "proxy", "studies_before_evict": int(before),
           "studies_after_evict": int(after)},
          open(path, "w", encoding="utf-8"), ensure_ascii=False)
PY

systemctl stop dicorina
sync
touch "$BARRIER/ready_proxy_done"
touch "$R/proxy-done"
