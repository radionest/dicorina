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

# Refuse a tmpfs WORK: the multi-GB golden/overlay qcow2 would live in RAM and starve the guests.
# mkdir first so the stat check evaluates the intended path even on a not-yet-created WORK.
mkdir -p "$WORK"
if [ "$(stat -f -c %T "$WORK" 2>/dev/null)" = tmpfs ]; then
  echo "FATAL: WORK=$WORK is on tmpfs (RAM-backed). Use a disk path, e.g. WORK=/var/tmp/dicorina-vm-net." >&2
  exit 1
fi

# change 7: preflight checks only the three images that actually exist (no proxy golden)
for img in "$WORK/pacs-golden.qcow2" "$WORK/client-golden.qcow2" "$BUSTER"; do
  [ -f "$img" ] || { echo "missing $img — run: bash staging/vm-net/build-golden.sh"; exit 1; }
done

# preflight: if the PACS golden bake recorded its instance count, verify it matches this run's expected total.
# Read from $WORK (alongside the golden, never wiped) — not $DATA, which rm -rf clears below, so the
# $DATA copy would survive only the first run and miss the "changed INSTANCES, no rebuild" footgun on run #2+.
_pacs_count_file="$WORK/pacs-golden-count.txt"
if [ -f "$_pacs_count_file" ]; then
  _want=$((STUDIES * INSTANCES))
  _got="$(cat "$_pacs_count_file")"
  if [ "$_got" != "$_want" ]; then
    echo "FATAL: PACS golden baked $_got instances but this run expects $_want (STUDIES=$STUDIES INSTANCES_PER_STUDY=$INSTANCES)."
    echo "  Rebuild: FORCE_REBUILD=pacs bash staging/vm-net/build-golden.sh"
    echo "  Or match: export INSTANCES_PER_STUDY=$_got before run.sh"
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
instance-id: vmnet-$name
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
  # Robust per-NIC setup by MAC, run at the top of role.sh: cloud-init's v2 network
  # config does not render on Buster, and the golden images carry a stale
  # /etc/network/interfaces that leaves the LAN NIC without its IPv4. Assign the LAN
  # static IP and DHCP the NAT NIC directly from /sys/class/net.
  # change 2: netup also mounts the dimsechord 9p share (harmless on non-proxy nodes)
  local netup="for _a in /sys/class/net/*/address; do _m=\$(cat \"\$_a\"); _i=\$(basename \"\$(dirname \"\$_a\")\"); [ \"\$_i\" = lo ] && continue; if [ \"\$_m\" = \"$lanmac\" ]; then ip link set \"\$_i\" up; ip addr add $ip/24 dev \"\$_i\" 2>/dev/null; fi; if [ \"\$_m\" = \"$natmac\" ]; then ip link set \"\$_i\" up; dhclient \"\$_i\" 2>/dev/null & fi; done; sleep 3; mkdir -p /repo; modprobe 9p 2>/dev/null||true; modprobe 9pnet_virtio 2>/dev/null||true; mountpoint -q /repo || mount -t 9p -o trans=virtio,version=9p2000.L,msize=104857600,access=any repo /repo 2>/dev/null; mkdir -p /dimsechord; mountpoint -q /dimsechord || mount -t 9p -o trans=virtio,version=9p2000.L,msize=104857600,access=any dimsechord /dimsechord 2>/dev/null; { echo == $name ==; ip -br addr; echo ROUTE; ip route; echo CONN; python3 -c \"import socket; s=socket.socket(); s.settimeout(4); print('proxy8042', s.connect_ex(('10.0.0.20',8042))); s2=socket.socket(); s2.settimeout(4); print('pacs4242', s2.connect_ex(('10.0.0.10',4242)))\"; } > /repo/staging/.data/vm-net/netdiag-$name.txt 2>&1"
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

  # change 2: expose the dimsechord sibling repo via a second 9p device
  qemu-system-x86_64 -enable-kvm -m "$mem" -smp 2 \
    -drive file="$overlay",if=virtio,format=qcow2 \
    -drive file="$WORK/seed-$name.iso",if=virtio,format=raw \
    -fsdev local,id=repo,path="$REPO",security_model=mapped-xattr \
    -device virtio-9p-pci,fsdev=repo,mount_tag=repo \
    -fsdev local,id=dch,path="$REPO/../dimsechord",security_model=mapped-xattr \
    -device virtio-9p-pci,fsdev=dch,mount_tag=dimsechord \
    -netdev user,id=nat -device virtio-net-pci,netdev=nat,mac="$natmac" \
    -netdev socket,mcast="$MCAST",id=lan -device virtio-net-pci,netdev=lan,mac="$lanmac" \
    -serial file:"$DATA/$name-console.log" -display none -pidfile "$WORK/$name.pid" &
  PIDS+=($!)
  echo "booted $name ($ip)"
}

# change 2: MNT helper also mounts dimsechord (harmless on non-proxy nodes)
MNT='mkdir -p /repo; modprobe 9p 2>/dev/null||true; modprobe 9pnet_virtio 2>/dev/null||true; mount -t 9p -o trans=virtio,version=9p2000.L,msize=104857600,access=any repo /repo; mountpoint -q /repo || true; mkdir -p /dimsechord; mountpoint -q /dimsechord || mount -t 9p -o trans=virtio,version=9p2000.L,msize=104857600,access=any dimsechord /dimsechord || true'

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
