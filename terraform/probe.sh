#!/usr/bin/env bash
# C211-I120857 terraform data.external wrapper (kedacore Azure managed-container GPU probe).
# stdout must be exactly one JSON object (terraform external contract); diagnostics on stderr.
set +e
cd "$(dirname "$0")" || exit 1
python3 ./probe_aci.py
RC=$?
if [ $RC -ne 0 ]; then
  echo '{"verdict":"PROBE_PROCESS_ERROR"}'
fi
exit 0
