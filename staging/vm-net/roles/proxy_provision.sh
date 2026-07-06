#!/bin/bash
# Runs inside the proxy VM (e2e). Shared install via proxy_install.sh, then the
# e2e-specific tail: wait for both clients, probe eviction, record proxy.json.
set -x
R=/repo/staging/.data/vm-net
BARRIER="$R/barrier"
mkdir -p "$R" "$BARRIER"
exec > >(tee -a "$R/proxy-provision.log" /dev/ttyS0) 2>&1

if ! bash /repo/staging/vm-net/roles/proxy_install.sh /repo/staging/vm-net/config/proxy.toml; then
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
