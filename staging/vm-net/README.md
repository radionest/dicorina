# vm-net — multi-machine e2e harness (dicorina)

Four QEMU/KVM VMs on an isolated socket-multicast LAN, exercising **dicorina** across real machine
boundaries — clients reach the PACS **only through the proxy**.

| node | IP | base image | role |
|------|----|------------|------|
| `pacs`    | 10.0.0.10 | golden (LSB Orthanc 1.12.11, 6×50 instances baked in) | upstream PACS `HOSPITALPACS` |
| `proxy`   | 10.0.0.20 | **rebuilt every run** (Debian + `deploy/install.sh` + systemd) | dicorina under test, AET pool `DICORINA1 DICORINA2` |
| `clienta` | 10.0.0.31 | golden (pydicom/pynetdicom) | SCU `CLIENTA` |
| `clientb` | 10.0.0.32 | golden (pydicom/pynetdicom) | SCU `CLIENTB` |

```bash
WORK=/var/tmp/dicorina-vm-net bash staging/vm-net/build-golden.sh   # once; FORCE_REBUILD=pacs|client|all to rebuild
WORK=/var/tmp/dicorina-vm-net bash staging/vm-net/run.sh            # boot all 4, run S0–S7, assert on the host
```

**`WORK` must be disk-backed**, not tmpfs — golden/overlay qcow2 on tmpfs would consume RAM and starve the
guests (the scripts now refuse a tmpfs `WORK`). `INSTANCES_PER_STUDY` must match between build and run.

## How it works

- **Rootless networking.** All four VMs share one L2 segment via QEMU socket multicast; each also has a
  user-mode NAT NIC for package pulls. Static LAN IPs are assigned by MAC inside each role script (cloud-init
  v2 net-config does not render on the Debian base; nodes are addressed by IP, not hostname).
- **PACS = LSB Orthanc** (no plugins). Studies are generated and bulk-imported once at golden-build time and
  baked into `/var/lib/orthanc/db`.
- **proxy = the real artifact.** Rebuilt each run from `deploy/install.sh`; the sibling `dimsechord` is exposed
  over a second 9p device (`mount_tag=dimsechord`) and copied to `/opt/dimsechord` so the path-dep resolves.
- **Coordination + results** flow through the 9p-shared `staging/.data/vm-net/` (gitignored): per-role JSON,
  9p barrier files, console logs, `netdiag-<node>.txt`.
- **Host gate.** `run.sh` waits for `proxy-done`, then runs `test_vm_net.py` on the host — it asserts over the
  collected JSON; it does not speak DICOM itself.

## Scenarios (asserted by `test_vm_net.py`)

- **S0** isolation — direct clienta→PACS association rejected (PACS knows only the proxy).
- **S1** QIDO-live — clientb lists all studies via the proxy; `PatientName=Иванов*` filters to the cyrillic study.
- **S2** WADO move-to-self — clientb reads study2 metadata (count == N) + a frame (non-empty body).
- **S3** DIMSE pass-through — clienta C-MOVE(study3) via the proxy lands all N instances; C-MOVE to `GHOST` refused.
- **S4** cyrillic both faces — DIMSE C-FIND (clienta) + HTTP QIDO (clientb) round-trip the cyrillic name.
- **S5** AET-pool concurrency — clienta↔study4 ∥ clientb↔study5, barrier-synced, each complete, no cross-contamination.
- **S6** cross-face cache — clienta warms study6 via DIMSE; clientb reads it via HTTP (count == N).
- **S7** eviction — short TTL + tiny cap drops the cached study count.

S5/S6 PACS move-counts and the S7 `Evicted` log line are recorded as observations, not gating asserts.

## Resource budget

pacs 3072 MB · proxy 2048 MB · clientA/B 1024 MB each ≈ 7 GB of guests; fits in a 23 GB host with headroom.
