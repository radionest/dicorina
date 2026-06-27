#!/usr/bin/env bash
# Build the two cached golden images for the vm-net harness. Idempotent: a re-run SKIPS any
# golden that already exists. To rebuild (e.g. after changing gen_studies.py or the instance
# count) set FORCE_REBUILD=pacs|client|all. run.sh boots fresh overlays over these; the proxy
# is NOT golden — it is rebuilt every run.
#
#   pacs-golden.qcow2    Debian Buster + LSB Orthanc 1.12.11, STUDIES×INSTANCES_PER_STUDY instances baked into /var/lib/orthanc/db
#   client-golden.qcow2  Debian Buster + pydicom + pynetdicom (the SCU agent runtime)
#
# Usage: bash staging/vm-net/build-golden.sh [--check]
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=/dev/null
. "$REPO/staging/vm-net/net.env"
WORK="${WORK:-/tmp/dicorina-vm-net}"
TIMEOUT="${TIMEOUT:-2400}"
STUDIES="${STUDIES:-6}"
INSTANCES="${INSTANCES_PER_STUDY:-50}"
IMG_URL="https://cloud.debian.org/images/cloud/buster/latest/debian-10-generic-amd64.qcow2"
BASE="$WORK/buster.qcow2"

if [ "${1:-}" = "--check" ]; then
  for t in qemu-system-x86_64 qemu-img cloud-localds; do command -v "$t" >/dev/null || { echo "missing $t"; exit 1; }; done
  [ -e /dev/kvm ] || { echo "missing /dev/kvm"; exit 1; }
  echo "prerequisites OK"; exit 0
fi

mkdir -p "$WORK"
if [ ! -f "$BASE" ]; then
  echo "Downloading Debian 10 Buster cloud image (resumable)..."
  ok=0
  for i in $(seq 1 30); do
    if curl -fL --retry 5 --retry-delay 3 --retry-all-errors -C - "$IMG_URL" -o "$BASE.part"; then ok=1; break; fi
    echo "  ... resuming ($i)"; sleep 3
  done
  [ "$ok" = 1 ] || { echo "image download failed"; exit 1; }
  mv "$BASE.part" "$BASE"
fi

build_one() {
  local name="$1" userdata_file="$2"
  local out="$WORK/$name-golden.qcow2" overlay="$WORK/$name-build.qcow2"
  if [ -f "$out" ] && [ "${FORCE_REBUILD:-}" != "all" ] && [ "${FORCE_REBUILD:-}" != "$name" ]; then
    echo "=== $name golden exists, skipping (FORCE_REBUILD=$name|all to rebuild) ==="; return 0
  fi
  echo "=== building $name golden ==="
  rm -f "$overlay" "$out.tmp"
  qemu-img create -f qcow2 -b "$BASE" -F qcow2 "$overlay" 20G >/dev/null
  cat > "$WORK/meta-data" <<EOF
instance-id: $name-golden
local-hostname: $name
EOF
  cloud-localds "$WORK/seed-$name.iso" "$userdata_file" "$WORK/meta-data"
  qemu-system-x86_64 -enable-kvm -m 2048 -smp 2 \
    -drive file="$overlay",if=virtio,format=qcow2 \
    -drive file="$WORK/seed-$name.iso",if=virtio,format=raw \
    -fsdev local,id=repo,path="$REPO",security_model=mapped-xattr \
    -device virtio-9p-pci,fsdev=repo,mount_tag=repo \
    -netdev user,id=n0 -device virtio-net-pci,netdev=n0 \
    -serial file:"$WORK/$name-build.log" -display none &
  local qpid=$! t=0 timed_out=0
  while kill -0 "$qpid" 2>/dev/null; do
    [ "$t" -ge "$TIMEOUT" ] && { echo "golden $name TIMEOUT"; kill "$qpid" 2>/dev/null || true; timed_out=1; break; }
    sleep 10; t=$((t + 10))
  done
  wait "$qpid" 2>/dev/null || true
  if [ "$timed_out" = 1 ]; then
    echo "golden $name FAILED (timeout) — keeping any previous $out"
    return 1
  fi
  qemu-img convert -O qcow2 "$overlay" "$out.tmp"
  echo "built $out.tmp (pending promotion)"
}

# --- PACS golden user-data: install orthanc, generate + import studies, poweroff ---
cat > "$WORK/ud-pacs" <<EOF
#cloud-config
write_files:
  - path: /root/seed-pacs.sh
    permissions: '0755'
    content: |
      #!/bin/bash
      set -x
      mkdir -p /repo
      modprobe 9p 2>/dev/null || true; modprobe 9pnet_virtio 2>/dev/null || true
      mount -t 9p -o trans=virtio,version=9p2000.L,msize=104857600,access=any repo /repo
      cat > /etc/apt/sources.list <<'SRC'
      deb http://archive.debian.org/debian buster main
      deb http://archive.debian.org/debian-security buster/updates main
      SRC
      apt-get -o Acquire::Check-Valid-Until=false update
      DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \\
        python3-pip python3-numpy curl ca-certificates
      python3 -m pip install "pydicom==2.4.4"
      install -d /opt/orthanc/bin
      curl -fsSL https://orthanc.uclouvain.be/downloads/linux-standard-base/orthanc/1.12.11/Orthanc \\
        -o /opt/orthanc/bin/Orthanc
      chmod +x /opt/orthanc/bin/Orthanc
      PYTHONPATH=/repo/staging/vm-net python3 /repo/staging/vm-net/gen_studies.py \\
        --out /var/lib/vmnet-studies --studies ${STUDIES} --instances ${INSTANCES}
      # Import into a tmpfs-backed store first (Orthanc fsyncs SQLite per stored instance,
      # ~1s each on the qcow2 disk under nested virt), then copy the finished store to the
      # on-disk /var/lib/orthanc/db that the runtime config/pacs.json reads.
      mkdir -p /var/lib/orthanc/db /var/lib/orthanc/db-ram
      mount -t tmpfs -o size=1g tmpfs /var/lib/orthanc/db-ram
      cat > /root/build-pacs.json <<'OC'
      { "StorageDirectory": "/var/lib/orthanc/db-ram", "IndexDirectory": "/var/lib/orthanc/db-ram",
        "HttpPort": 8042, "DicomAet": "HOSPITALPACS", "DefaultEncoding": "Utf8",
        "RemoteAccessAllowed": true, "AuthenticationEnabled": false }
      OC
      /opt/orthanc/bin/Orthanc /root/build-pacs.json > /var/log/orthanc-build.log 2>&1 &
      OPID=\$!
      sleep 6
      # Bulk import: Orthanc's POST /instances accepts a ZIP archive and imports every
      # DICOM inside it in ONE request — far cheaper than one POST per instance. Use
      # 127.0.0.1 (not localhost) to skip a per-request IPv6->IPv4 fallback delay.
      python3 -c "import zipfile, glob, os; z = zipfile.ZipFile('/tmp/studies.zip', 'w', zipfile.ZIP_STORED); [z.write(f, os.path.basename(f)) for f in glob.glob('/var/lib/vmnet-studies/*.dcm')]; z.close()"
      curl -s -X POST http://127.0.0.1:8042/instances --data-binary @/tmp/studies.zip > /tmp/import.json
      rm -f /tmp/studies.zip
      curl -s http://127.0.0.1:8042/statistics
      # record the baked instance count where the host build script can verify it
      mkdir -p /repo/staging/.data/vm-net
      curl -s http://127.0.0.1:8042/statistics | python3 -c "import sys, json; print(json.load(sys.stdin).get('CountInstances', 0))" > /repo/staging/.data/vm-net/pacs-golden-count.txt
      kill \$OPID 2>/dev/null || true; sleep 2
      cp -a /var/lib/orthanc/db-ram/. /var/lib/orthanc/db/
      umount /var/lib/orthanc/db-ram; rmdir /var/lib/orthanc/db-ram
      rm -rf /var/lib/vmnet-studies
      sync
runcmd:
  - [ bash, /root/seed-pacs.sh ]
power_state: { mode: poweroff, timeout: 60, condition: true }
EOF

# --- client golden user-data: pydicom + pynetdicom, poweroff ---
cat > "$WORK/ud-client" <<'EOF'
#cloud-config
write_files:
  - path: /root/seed-client.sh
    permissions: '0755'
    content: |
      #!/bin/bash
      set -x
      cat > /etc/apt/sources.list <<'SRC'
      deb http://archive.debian.org/debian buster main
      deb http://archive.debian.org/debian-security buster/updates main
      SRC
      apt-get -o Acquire::Check-Valid-Until=false update
      DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        python3-pip python3-numpy
      python3 -m pip install "pydicom==2.4.4" "pynetdicom==2.0.2"
      sync
runcmd:
  - [ bash, /root/seed-client.sh ]
power_state: { mode: poweroff, timeout: 60, condition: true }
EOF

rm -f "$REPO/staging/.data/vm-net/pacs-golden-count.txt"
build_one pacs "$WORK/ud-pacs"
# Promote the freshly built PACS golden only after verifying the bulk ZIP import baked the
# expected count (otherwise a broken import would silently ship). On mismatch, discard the
# rebuild and keep any previous good golden, failing the build now rather than 60 min into a run.
if [ -f "$WORK/pacs-golden.qcow2.tmp" ]; then
  want=$((STUDIES * INSTANCES))
  got="$(cat "$REPO/staging/.data/vm-net/pacs-golden-count.txt" 2>/dev/null || echo 0)"
  if [ "$got" != "$want" ]; then
    echo "FATAL: PACS golden baked $got instances, expected $want — discarding rebuild, keeping previous"
    rm -f "$WORK/pacs-golden.qcow2.tmp"
    exit 1
  fi
  mv -f "$WORK/pacs-golden.qcow2.tmp" "$WORK/pacs-golden.qcow2"
  echo "PACS golden import verified: $got instances"
fi
build_one client "$WORK/ud-client"
[ -f "$WORK/client-golden.qcow2.tmp" ] && mv -f "$WORK/client-golden.qcow2.tmp" "$WORK/client-golden.qcow2"
echo "goldens ready in $WORK"
