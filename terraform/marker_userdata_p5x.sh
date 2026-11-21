#!/bin/bash
# c211-i121988 P-family marker user-data.
# Waits for the NVIDIA driver, prints the REAL device identity (nvidia-smi UUID)
# to the serial console and /dev/console, then powers the instance off
# (InstanceInitiatedShutdownBehavior=terminate) so no resource can leak even if
# the probe loses its job. Every write is individually guarded.
log() { printf '%s\n' "$*" >> /dev/console 2>/dev/null; printf '%s\n' "$*" >> /dev/ttyS0 2>/dev/null; }

log "C211_MARKER_START $(date -u +%Y-%m-%dT%H:%M:%SZ) host=$(hostname)"
i=0
while [ $i -lt 90 ]; do
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/tmp/nvsmi.txt 2>/tmp/nvsmi.err; then
    break
  fi
  i=$((i+1))
  sleep 5
done
log "C211_MARKER_UUID_BEGIN attempts=$i"
log "$(cat /tmp/nvsmi.txt 2>/dev/null)"
log "$(nvidia-smi --query-gpu=uuid,name,memory.total,driver_version --format=csv,noheader 2>&1)"
log "C211_MARKER_PROC"
log "$(cat /proc/driver/nvidia/gpus/*/information 2>/dev/null)"
log "C211_MARKER_DEVS"
log "$(ls -l /dev/nvidia* 2>/dev/null)"
log "C211_MARKER_LSPCI"
log "$(lspci -nn 2>/dev/null | grep -i -E 'nvidia|3d controller' | head -8)"
log "C211_MARKER_END $(date -u +%Y-%m-%dT%H:%M:%SZ)"
sync
sleep 10
shutdown -h now
