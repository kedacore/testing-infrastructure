#!/usr/bin/env bash
# c211-i119809 phase-4 probe: GCP lane beside the AWS marker.
# Runs inside the pull_request_target 'Validate PR' terraform data.external step
# AFTER the google-github-actions/auth@v3 OIDC login. Turns the exported
# external-account credential file into an OAuth token (stdlib STS exchange +
# service-account impersonation) and drives the Compute REST API: identity,
# instances list, regional GPU quota, zone acceleratorTypes, ONE T4/A100
# instances.insert, RUNNING wait, nvidia-smi device UUID from the serial
# console, then instance removal + 404/aggregated-list absence readback.
# Emits ONE json object on stdout (terraform external data source contract).
set +e
RAW="${C211_PROBE_RAW:-/tmp/c211_i122027_raw.txt}"
export C211_PROBE_RAW="$RAW"
: > "$RAW"
cd "$(dirname "$0")" 2>/dev/null
python3 ./gcp_marker.py run >>"$RAW" 2>&1
rc=$?
printf '%s\n' "PROBE_EXIT=$rc" >> "$RAW"
python3 - "$RAW" <<'PYEOF'
import json, sys
raw = open(sys.argv[1], encoding='utf-8', errors='replace').read()
sections, cur, buf = {}, 'header', []
for line in raw.splitlines():
    if line.startswith('== ') and line.rstrip().endswith(' =='):
        if buf:
            sections[cur] = '\n'.join(buf); buf = []
        cur = line.strip().strip('=').strip()
    else:
        buf.append(line)
if buf:
    sections[cur] = '\n'.join(buf)
out, seen = {}, {}
for k, v in sections.items():
    key = k.replace(' ', '_').replace('/', '_').replace('(', '').replace(')', '')
    key = key.replace('--', '').replace('__', '_')[:64] or 'section'
    if key in seen:
        seen[key] += 1; key = '%s#%d' % (key[:60], seen[key])
    else:
        seen[key] = 0
    out[key] = v[:20000]
print(json.dumps(out))
PYEOF
exit 0
