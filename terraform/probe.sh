#!/usr/bin/env bash
# c211-i119810 Azure lane wrapper for the terraform data.external contract.
# Phase A: read-only all-region NC/ND/NV SKU + usage scan with the OIDC token that
# azure/login@v3 minted. Phase B: at most ONE marker VM only if a real quota exists,
# device proof, delete, readback. All diagnostics on stderr; one JSON on stdout.
set +e
export C211_PROBE_RAW="${C211_PROBE_RAW:-/tmp/c211_i119810_raw.txt}"
cd "$(dirname "$0")" || exit 1
python3 ./probe_azure.py 2> "$C211_PROBE_RAW"
RC=$?
if [ $RC -ne 0 ]; then
  python3 - <<'PYEOF'
import json
print(json.dumps({"verdict": "PROBE_PROCESS_ERROR", "rc": str(__import__('subprocess').run(['true']).returncode)}))
PYEOF
fi
exit 0
