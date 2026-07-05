#!/usr/bin/env bash
# Latency bench: boots 3 VMs (pacs with DICOMweb + CLIENTA as a known modality, proxy,
# clienta) and measures per-scenario latency direct vs through dicorina from one client
# VM. Pure measurement: exit code reflects only whether the bench produced data.
#
# boot_node + preflights are copied from run.sh (e2e-frozen); keep in sync manually.
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

mkdir -p "$WORK"
if [ "$(stat -f -c %T "$WORK" 2>/dev/null)" = tmpfs ]; then
  echo "FATAL: WORK=$WORK is on tmpfs (RAM-backed). Use a disk path." >&2
  exit 1
fi

for img in "$WORK/pacs-golden.qcow2" "$WORK/client-golden.qcow2" "$BUSTER"; do
  [ -f "$img" ] || { echo "missing $img — run: bash staging/vm-net/build-golden.sh"; exit 1; }
done

_pacs_count_file="$WORK/pacs-golden-count.txt"
if [ -f "$_pacs_count_file" ]; then
  _want=$((STUDIES * INSTANCES))
  _got="$(cat "$_pacs_count_file")"
  if [ "$_got" != "$_want" ]; then
    echo "FATAL: PACS golden baked $_got instances but this run expects $_want."
    echo "  Rebuild: FORCE_REBUILD=pacs bash staging/vm-net/build-golden.sh"
    echo "  Or match: export INSTANCES_PER_STUDY=$_got before bench.sh"
    exit 1
  fi
fi

rm -rf "$DATA"; mkdir -p "$DATA" "$BARRIER"
PIDS=()
cleanup() { for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null || true; done; }
trap cleanup EXIT

# boot_node <name> <base-image> <ip> <lan_mac> <nat_mac> <mem> <run-script-body>
boot_node() {
  local name="$1" base="$2" ip="$3" lanmac="$4" natmac="$5" mem="$6" body="$7"
  local overlay="$WORK/$name-overlay.qcow2"
  rm -f "$overlay"
  qemu-img create -f qcow2 -b "$base" -F qcow2 "$overlay" 20G >/dev/null

  cat > "$WORK/meta-$name" <<EOF
instance-id: vmnet-bench-$name
local-hostname: $name
EOF
  cat > "$WORK/netcfg-$name" <<EOF
version: 2
ethernets:
  nat:
    match: { macaddress: "$natmac" }
    dhcp4: true
  lan:
    match: { macaddress: "$lanmac" }
    addresses: [ $ip/24 ]
EOF
  local netup="for _a in /sys/class/net/*/address; do _m=\$(cat \"\$_a\"); _i=\$(basename \"\$(dirname \"\$_a\")\"); [ \"\$_i\" = lo ] && continue; if [ \"\$_m\" = \"$lanmac\" ]; then ip link set \"\$_i\" up; ip addr add $ip/24 dev \"\$_i\" 2>/dev/null; fi; if [ \"\$_m\" = \"$natmac\" ]; then ip link set \"\$_i\" up; dhclient \"\$_i\" 2>/dev/null & fi; done; sleep 3; mkdir -p /repo; modprobe 9p 2>/dev/null||true; modprobe 9pnet_virtio 2>/dev/null||true; mountpoint -q /repo || mount -t 9p -o trans=virtio,version=9p2000.L,msize=104857600,access=any repo /repo 2>/dev/null"
  local role="#!/bin/bash
$netup
${body#\#!/bin/bash}"
  { echo "#cloud-config"
    echo "write_files:"
    echo "  - path: /root/role.sh"
    echo "    permissions: '0755'"
    echo "    content: |"
    printf '%s\n' "$role" | sed 's/^/      /'
    echo "runcmd:"
    echo "  - [ bash, /root/role.sh ]"
  } > "$WORK/ud-$name"

  cloud-localds --network-config "$WORK/netcfg-$name" "$WORK/seed-$name.iso" "$WORK/ud-$name" "$WORK/meta-$name"

  qemu-system-x86_64 -enable-kvm -m "$mem" -smp 2 \
    -drive file="$overlay",if=virtio,format=qcow2 \
    -drive file="$WORK/seed-$name.iso",if=virtio,format=raw \
    -fsdev local,id=repo,path="$REPO",security_model=mapped-xattr \
    -device virtio-9p-pci,fsdev=repo,mount_tag=repo \
    -netdev user,id=nat -device virtio-net-pci,netdev=nat,mac="$natmac" \
    -netdev socket,mcast="$MCAST",id=lan -device virtio-net-pci,netdev=lan,mac="$lanmac" \
    -serial file:"$DATA/$name-console.log" -display none -pidfile "$WORK/$name.pid" &
  PIDS+=($!)
  echo "booted $name ($ip)"
}

MNT='mkdir -p /repo; modprobe 9p 2>/dev/null||true; modprobe 9pnet_virtio 2>/dev/null||true; mount -t 9p -o trans=virtio,version=9p2000.L,msize=104857600,access=any repo /repo; mountpoint -q /repo || true'

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
