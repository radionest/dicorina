#!/bin/bash
# Cache-wipe watcher for the latency bench. Runs inside the proxy VM after dicorina is
# healthy. Protocol over the 9p data dir: on bench-wipe-req-<k> — stop dicorina, wipe
# /var/cache/dicorina, start, wait healthy, touch bench-wipe-ack-<k>. Requests are
# strictly sequential (k = 1,2,3...). Exits when bench-stop appears.
set -u
R=/repo/staging/.data/vm-net
k=1
while [ ! -f "$R/bench-stop" ]; do
  if [ -f "$R/bench-wipe-req-$k" ] && [ ! -f "$R/bench-wipe-ack-$k" ]; then
    systemctl stop dicorina
    rm -rf /var/cache/dicorina/*
    systemctl start dicorina
    healthy=0
    for _ in $(seq 1 60); do
      curl -fsS http://localhost:8042/health >/dev/null 2>&1 && { healthy=1; break; }
      sleep 2
    done
    # ack only a healthy restart; otherwise leave a host-visible failure marker so
    # the agent's ack timeout points at the restart, not at the protocol
    if [ "$healthy" = 1 ]; then
      touch "$R/bench-wipe-ack-$k"
    else
      echo "wipe $k: dicorina not healthy 120s after restart" > "$R/bench-wipe-failed-$k"
    fi
    k=$((k + 1))
  fi
  sleep 1
done
