#!/usr/bin/env bash
# Boot the 4-VM vm-net DICOM network and run the e2e scenarios. PACS + clients boot
# from cached goldens (build-golden.sh); the proxy is rebuilt from deploy/install.sh
# every run. VMs share an isolated socket-multicast LAN; each also has a user-mode NAT
# NIC for package pulls. Roles, coordination and results flow through the 9p-shared
# staging/.data/vm-net/ directory. The host then asserts over the collected JSON.
#
# Usage: bash staging/vm-net/run.sh
# Env:   WORK=<dir>  TIMEOUT=<seconds>  INSTANCES_PER_STUDY=<n>
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=/dev/null
. "$REPO/staging/vm-net/net.env"
WORK="${WORK:-/var/tmp/dicorina-vm-net}"          # change 1
TIMEOUT="${TIMEOUT:-2400}"
INSTANCES="${INSTANCES_PER_STUDY:-50}"
BUSTER="$WORK/buster.qcow2"
DATA="$REPO/staging/.data/vm-net"
BARRIER="$DATA/barrier"

# change 7: preflights (tmpfs guard, three-image check, pacs-count check) and boot_node
# are shared with bench.sh — single source in vm_common.sh
# shellcheck source=/dev/null
. "$REPO/staging/vm-net/vm_common.sh"
VMNET_NETDIAG=1   # per-VM network dump into netdiag-<name>.txt (feeds e2e debugging)
vmnet_preflight

rm -rf "$DATA"; mkdir -p "$DATA" "$BARRIER"
PIDS=()
cleanup() { for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT

# --- PACS: start distro Orthanc with the tightened vm-net config (data is baked in) ---
# change 3: --verbose so accepted incoming associations log their calling AET; the host then
# parses pacs-orthanc.log for the S5/S6 distinct-pool-caller observation (default level logs
# only warnings/errors, so the calling AET of a successful C-MOVE is otherwise never recorded).
boot_node pacs "$WORK/pacs-golden.qcow2" "$PACS_IP" "$PACS_LAN_MAC" "$PACS_NAT_MAC" 3072 \
"#!/bin/bash
set -x
$MNT
systemctl stop orthanc 2>/dev/null || true
/opt/orthanc/bin/Orthanc --verbose /repo/staging/vm-net/config/pacs.json > /repo/staging/.data/vm-net/pacs-orthanc.log 2>&1 &
OPID=\$!
sleep 6
curl -s http://localhost:8042/statistics > /repo/staging/.data/vm-net/pacs-stats.json
touch /repo/staging/.data/vm-net/pacs-done
wait \$OPID"

# --- proxy: rebuild from plain Buster base every run (no proxy golden) ---
# change 4: boot from buster.qcow2, not a golden; provision via proxy_provision.sh; mem 2048
# NOTE: if uv's managed Python or a wheel needs glibc > 2.28 (Buster), fall back to
# a Bookworm cloud image for the proxy ONLY — keep PACS/client goldens on Buster.
boot_node proxy "$BUSTER" "$PROXY_IP" "$PROXY_LAN_MAC" "$PROXY_NAT_MAC" 2048 \
"#!/bin/bash
$MNT
bash /repo/staging/vm-net/roles/proxy_provision.sh"

# --- clients: run the SCU agent with role env ---
# change 5: export dicorina-specific proxy env names; add STUDIES
client_body() {  # $1 role, $2 self_aet, $3 scp_port
  echo "#!/bin/bash
set -x
$MNT
export ROLE=$1 SELF_AET=$2 SCP_PORT=$3
export PROXY_HOST=$PROXY_IP PROXY_DIMSE=$PROXY_DIMSE PROXY_HTTP=$PROXY_HTTP PROXY_CALLED_AET=$PROXY_CALLED_AET
export PACS_HOST=$PACS_IP PACS_AET=$PACS_AET PACS_DICOM=$PACS_DICOM
export BARRIER_DIR=/repo/staging/.data/vm-net/barrier RESULT_PATH=/repo/staging/.data/vm-net/$1.json
export STUDIES=$STUDIES INSTANCES_PER_STUDY=$INSTANCES
python3 /repo/staging/vm-net/roles/client_agent.py
touch /repo/staging/.data/vm-net/$1-agent-done"
}
boot_node clienta "$WORK/client-golden.qcow2" "$CLIENTA_IP" "$CLIENTA_LAN_MAC" "$CLIENTA_NAT_MAC" 1024 "$(client_body clienta "$CLIENTA_AET" "$CLIENTA_SCP")"
boot_node clientb "$WORK/client-golden.qcow2" "$CLIENTB_IP" "$CLIENTB_LAN_MAC" "$CLIENTB_NAT_MAC" 1024 "$(client_body clientb "$CLIENTB_AET" "$CLIENTB_SCP")"

echo "Waiting for proxy-done (timeout ${TIMEOUT}s)..."
elapsed=0
while [ ! -f "$DATA/proxy-done" ]; do
  [ "$elapsed" -ge "$TIMEOUT" ] && { echo "TIMEOUT"; break; }
  sleep 10; elapsed=$((elapsed + 10))
done

# change 3: derive PACS observations from the Orthanc --verbose log (feeds S5/S6 only; never a
# hard gate). Orthanc logs each incoming C-MOVE as "Incoming Move request from AET <caller>";
# distinct callers prove the AET pool leased DICORINA1/DICORINA2 across the concurrent moves.
if [ -f "$DATA/pacs-orthanc.log" ]; then
  python3 - "$DATA/pacs-orthanc.log" "$DATA/pacs.json" <<'PY'
import json, re, sys
log = open(sys.argv[1], encoding="utf-8", errors="ignore").read()
callers = re.findall(r'Incoming Move request from AET ([A-Za-z0-9_-]+)', log)
json.dump({"role": "pacs", "move_requests": len(callers),
           "distinct_callers": sorted(set(callers))},
          open(sys.argv[2], "w", encoding="utf-8"), ensure_ascii=False)
PY
fi

echo "================ vm-net results ================"
ls -1 "$DATA"/*.json 2>/dev/null || echo "(no result JSON produced)"
if [ -f "$DATA/proxy-done" ]; then
  if ! command -v uv >/dev/null 2>&1; then
    echo "FATAL: 'uv' not found on the host — it runs the pytest gate"; exit 1
  fi
  # change 6: dicorina env names; add STUDIES; -rP surfaces S5/S6 PACS move-count
  # observations printed by passing tests (pytest hides stdout without -rP under -v)
  gate=0; VMNET_DATA="$DATA" INSTANCES_PER_STUDY="$INSTANCES" STUDIES="$STUDIES" \
    uv run --with pytest pytest --noconftest "$REPO/staging/vm-net/test_vm_net.py" -v -rP || gate=$?
  echo "host gate exit: $gate"
  exit "$gate"
else
  echo "run did NOT complete; inspect $DATA/*-console.log"; exit 1
fi
