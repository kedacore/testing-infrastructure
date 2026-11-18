#!/bin/bash
# c211-i109237 marker user-data: print the real GPU device identity to the EC2
# console output. No mining, no egress needed. Teardown is performed by the
# probe itself through the EC2 TerminateInstances API (api-driven cleanup).
exec > /dev/console 2>&1
echo "C211_MARKER_START $(date -u +%Y-%m-%dT%H:%M:%SZ) host=$(hostname)"
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi -L
  nvidia-smi --query-gpu=uuid,name,memory.total,driver_version --format=csv,noheader
else
  echo "C211_MARKER nvidia-smi-absent"
  lspci -nn 2>/dev/null | grep -i -E 'nvidia|3d controller' | head -8
fi
echo "C211_MARKER_END $(date -u +%Y-%m-%dT%H:%M:%SZ)"
exit 0
