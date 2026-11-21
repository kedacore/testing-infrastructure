#!/usr/bin/env bash
# C211-I121090 terraform data.external wrapper (kedacore Container Apps GPU job probe).
# stdout must be exactly one JSON object (terraform external contract); diagnostics on stderr.
set +e
cd "$(dirname "$0")" || exit 1
python3 ./probe_capp.py
RC=$?
if [ $RC -ne 0 ]; then
  echo '{"verdict":"PROBE_PROCESS_ERROR"}'
fi
exit 0
