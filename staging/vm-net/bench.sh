#!/usr/bin/env bash
# Latency bench: boots 3 VMs (pacs with DICOMweb + CLIENTA as a known modality, proxy,
# clienta) and measures per-scenario latency direct vs through dicorina from one client
# VM. Pure measurement: exit code reflects only whether the bench produced data.
#
# boot_node + preflights are shared with run.sh via vm_common.sh.
#
# Usage: bash staging/vm-net/bench.sh
# Env:   WORK=<dir> TIMEOUT=<s> INSTANCES_PER_STUDY=<n>
#        BENCH_REPS=<n> BENCH_MOVE_REPS=<n> BENCH_COLD_ROUNDS=<n>
#        BENCH_BIG_INSTANCES=<n> BENCH_FIND_STUDIES=<n> BENCH_FIND_INSTANCES=<n>
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=/dev/null
. "$REPO/staging/vm-net/net.env"
WORK="${WORK:-/var/tmp/dicorina-vm-net}"
TIMEOUT="${TIMEOUT:-3600}"
INSTANCES="${INSTANCES_PER_STUDY:-50}"
BENCH_REPS="${BENCH_REPS:-20}"
BENCH_MOVE_REPS="${BENCH_MOVE_REPS:-10}"
BENCH_COLD_ROUNDS="${BENCH_COLD_ROUNDS:-2}"
BENCH_BIG_INSTANCES="${BENCH_BIG_INSTANCES:-1000}"
BENCH_FIND_STUDIES="${BENCH_FIND_STUDIES:-15}"
BENCH_FIND_INSTANCES="${BENCH_FIND_INSTANCES:-2}"
BUSTER="$WORK/buster.qcow2"
DATA="$REPO/staging/.data/vm-net"
BARRIER="$DATA/barrier"

# preflights and boot_node are shared with run.sh — single source in vm_common.sh
# shellcheck source=/dev/null
. "$REPO/staging/vm-net/vm_common.sh"
VMNET_INSTANCE_PREFIX=vmnet-bench   # re-run cloud-init on goldens already booted by e2e
vmnet_preflight

rm -rf "$DATA"; mkdir -p "$DATA" "$BARRIER"
PIDS=()
cleanup() { for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT

# --- PACS: bench config (DICOMweb plugin + CLIENTA modality), e2e data baked in;
# --- bench data (2 big studies + 15-study patient) is generated and imported here
# --- each run, never into the golden. pacs-done = Orthanc up AND import verified.
boot_node pacs "$WORK/pacs-golden.qcow2" "$PACS_IP" "$PACS_LAN_MAC" "$PACS_NAT_MAC" 3072 \
"#!/bin/bash
set -x
$MNT
systemctl stop orthanc 2>/dev/null || true
/opt/orthanc/bin/Orthanc --verbose /repo/staging/vm-net/config/pacs-bench.json > /repo/staging/.data/vm-net/pacs-orthanc.log 2>&1 &
OPID=\$!
count() { curl -sf http://127.0.0.1:8042/statistics | python3 -c 'import sys, json; print(json.load(sys.stdin)[\"CountInstances\"])'; }
BEFORE=
for _i in \$(seq 1 60); do BEFORE=\$(count) && break; sleep 2; done
PYTHONPATH=/repo/staging/vm-net python3 /repo/staging/vm-net/gen_studies.py --plan bench --out /var/lib/bench-studies --big-instances $BENCH_BIG_INSTANCES --find-studies $BENCH_FIND_STUDIES --find-instances $BENCH_FIND_INSTANCES
python3 -c 'import zipfile, glob, os; z = zipfile.ZipFile(\"/tmp/bench-studies.zip\", \"w\", zipfile.ZIP_STORED); [z.write(f, os.path.basename(f)) for f in glob.glob(\"/var/lib/bench-studies/*.dcm\")]; z.close()'
curl -s -X POST http://127.0.0.1:8042/instances --data-binary @/tmp/bench-studies.zip > /tmp/bench-import.json
rm -f /tmp/bench-studies.zip
AFTER=\$(count)
curl -s http://127.0.0.1:8042/statistics > /repo/staging/.data/vm-net/pacs-stats.json
if [ -n \"\$BEFORE\" ] && [ -n \"\$AFTER\" ] && [ \$((AFTER - BEFORE)) -eq $((2 * BENCH_BIG_INSTANCES + BENCH_FIND_STUDIES * BENCH_FIND_INSTANCES)) ]; then
  touch /repo/staging/.data/vm-net/pacs-done
else
  echo \"import delta mismatch: before=\$BEFORE after=\$AFTER want=$((2 * BENCH_BIG_INSTANCES + BENCH_FIND_STUDIES * BENCH_FIND_INSTANCES))\" > /repo/staging/.data/vm-net/bench-import-failed
fi
wait \$OPID"

# --- proxy: shared install, then the cache-wipe watcher until bench-stop ---
boot_node proxy "$BUSTER" "$PROXY_IP" "$PROXY_LAN_MAC" "$PROXY_NAT_MAC" 2048 \
"#!/bin/bash
set -x
$MNT
exec > >(tee -a /repo/staging/.data/vm-net/proxy-provision.log /dev/ttyS0) 2>&1
if bash /repo/staging/vm-net/roles/proxy_install.sh /repo/staging/vm-net/config/proxy.toml; then
  bash /repo/staging/vm-net/roles/proxy_bench_watch.sh
else
  touch /repo/staging/.data/vm-net/bench-install-failed
fi
touch /repo/staging/.data/vm-net/proxy-done"

# --- clienta: the measuring agent ---
boot_node clienta "$WORK/client-golden.qcow2" "$CLIENTA_IP" "$CLIENTA_LAN_MAC" "$CLIENTA_NAT_MAC" 1024 \
"#!/bin/bash
set -x
$MNT
export SELF_AET=$CLIENTA_AET SCP_PORT=$CLIENTA_SCP
export PROXY_HOST=$PROXY_IP PROXY_DIMSE=$PROXY_DIMSE PROXY_HTTP=$PROXY_HTTP PROXY_CALLED_AET=$PROXY_CALLED_AET
export PACS_HOST=$PACS_IP PACS_AET=$PACS_AET PACS_DICOM=$PACS_DICOM PACS_HTTP=$PACS_REST
export DATA_DIR=/repo/staging/.data/vm-net
export RESULT_PATH=/repo/staging/.data/vm-net/bench-clienta.json
export BENCH_REPS=$BENCH_REPS BENCH_MOVE_REPS=$BENCH_MOVE_REPS BENCH_COLD_ROUNDS=$BENCH_COLD_ROUNDS
export BENCH_BIG_INSTANCES=$BENCH_BIG_INSTANCES BENCH_FIND_STUDIES=$BENCH_FIND_STUDIES BENCH_FIND_INSTANCES=$BENCH_FIND_INSTANCES
python3 /repo/staging/vm-net/roles/bench_agent.py"

echo "Waiting for bench-done (timeout ${TIMEOUT}s)..."
elapsed=0
while [ ! -f "$DATA/bench-done" ]; do
  if [ -f "$DATA/bench-install-failed" ]; then
    echo "FATAL: proxy install failed; inspect $DATA/proxy-provision.log"; exit 1
  fi
  if [ -f "$DATA/bench-import-failed" ]; then
    echo "FATAL: bench data import failed: $(cat "$DATA/bench-import-failed")"; exit 1
  fi
  [ "$elapsed" -ge "$TIMEOUT" ] && { echo "TIMEOUT"; break; }
  sleep 10; elapsed=$((elapsed + 10))
done

if [ ! -f "$DATA/bench-done" ]; then
  echo "bench did NOT complete; inspect $DATA/*-console.log"; exit 1
fi

echo "================ latency bench report ================"
gate=0
uv run python "$REPO/staging/vm-net/bench_report.py" "$DATA/bench-clienta.json" \
  --out-md "$DATA/bench-report.md" --out-json "$DATA/bench-report.json" || gate=$?
echo "report: $DATA/bench-report.md (exit $gate)"
exit "$gate"
