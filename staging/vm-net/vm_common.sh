#!/usr/bin/env bash
# Shared VM plumbing for the vm-net harnesses (run.sh e2e + bench.sh latency bench):
# host preflights and the boot_node cloud-init/QEMU launcher. Sourced, not executed.
# Caller must define: REPO, WORK, DATA, BUSTER, MCAST, STUDIES, INSTANCES, PIDS.
# Knobs (set before calling boot_node):
#   VMNET_INSTANCE_PREFIX  cloud-init instance-id prefix (default vmnet); bench.sh uses
#                          vmnet-bench so cloud-init re-runs role.sh on goldens that
#                          already booted under the e2e instance-id
#   VMNET_NETDIAG=1        append the per-VM network diagnostic dump to the role script

vmnet_preflight() {
  # Refuse a tmpfs WORK: the multi-GB golden/overlay qcow2 would live in RAM and starve
  # the guests. mkdir first so the stat check evaluates the intended path.
  mkdir -p "$WORK"
  if [ "$(stat -f -c %T "$WORK" 2>/dev/null)" = tmpfs ]; then
    echo "FATAL: WORK=$WORK is on tmpfs (RAM-backed). Use a disk path, e.g. WORK=/var/tmp/dicorina-vm-net." >&2
    exit 1
  fi

  local img
  for img in "$WORK/pacs-golden.qcow2" "$WORK/client-golden.qcow2" "$BUSTER"; do
    [ -f "$img" ] || { echo "missing $img — run: bash staging/vm-net/build-golden.sh"; exit 1; }
  done

  # If the PACS golden bake recorded its instance count, verify it matches this run.
  # Read from $WORK (alongside the golden, never wiped) — not $DATA, which the caller
  # rm -rf's each run, so a $DATA copy would miss the "changed INSTANCES, no rebuild"
  # footgun on run #2+.
  local count_file="$WORK/pacs-golden-count.txt" want got
  if [ -f "$count_file" ]; then
    want=$((STUDIES * INSTANCES))
    got="$(cat "$count_file")"
    if [ "$got" != "$want" ]; then
      echo "FATAL: PACS golden baked $got instances but this run expects $want (STUDIES=$STUDIES INSTANCES_PER_STUDY=$INSTANCES)."
      echo "  Rebuild: FORCE_REBUILD=pacs bash staging/vm-net/build-golden.sh"
      echo "  Or match: export STUDIES/INSTANCES_PER_STUDY so their product is $got"
      exit 1
    fi
  fi
}

# boot_node <name> <base-image> <ip> <lan_mac> <nat_mac> <mem> <run-script-body>
boot_node() {
  local name="$1" base="$2" ip="$3" lanmac="$4" natmac="$5" mem="$6" body="$7"
  local overlay="$WORK/$name-overlay.qcow2"
  rm -f "$overlay"
  qemu-img create -f qcow2 -b "$base" -F qcow2 "$overlay" 20G >/dev/null

  cat > "$WORK/meta-$name" <<EOF
instance-id: ${VMNET_INSTANCE_PREFIX:-vmnet}-$name
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
  local netup="for _a in /sys/class/net/*/address; do _m=\$(cat \"\$_a\"); _i=\$(basename \"\$(dirname \"\$_a\")\"); [ \"\$_i\" = lo ] && continue; if [ \"\$_m\" = \"$lanmac\" ]; then ip link set \"\$_i\" up; ip addr add $ip/24 dev \"\$_i\" 2>/dev/null; fi; if [ \"\$_m\" = \"$natmac\" ]; then ip link set \"\$_i\" up; dhclient \"\$_i\" 2>/dev/null & fi; done; sleep 3; mkdir -p /repo; modprobe 9p 2>/dev/null||true; modprobe 9pnet_virtio 2>/dev/null||true; mountpoint -q /repo || mount -t 9p -o trans=virtio,version=9p2000.L,msize=104857600,access=any repo /repo 2>/dev/null"
  if [ "${VMNET_NETDIAG:-0}" = 1 ]; then
    netup="$netup; { echo == $name ==; ip -br addr; echo ROUTE; ip route; echo CONN; python3 -c \"import socket; s=socket.socket(); s.settimeout(4); print('proxy8042', s.connect_ex(('10.0.0.20',8042))); s2=socket.socket(); s2.settimeout(4); print('pacs4242', s2.connect_ex(('10.0.0.10',4242)))\"; } > /repo/staging/.data/vm-net/netdiag-$name.txt 2>&1"
  fi
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

MNT='mkdir -p /repo; modprobe 9p 2>/dev/null||true; modprobe 9pnet_virtio 2>/dev/null||true; mountpoint -q /repo || mount -t 9p -o trans=virtio,version=9p2000.L,msize=104857600,access=any repo /repo; mountpoint -q /repo || { echo "FATAL: /repo 9p mount failed" >&2; exit 1; }'
