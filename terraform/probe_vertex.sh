#!/usr/bin/env bash
# c211-i122745 probe: Vertex AI customJobs delivery plane on cncf-keda-testing
# via the kedacore GitHub-OIDC WIF identity (roles/owner). Read-only quota /
# permission reads, then at most ONE nonce on-demand customJob (A100 first,
# else T4), one SPOT retry only after a verbatim on-demand quota refusal, device
# capture through Cloud Logging, delete + absence proof.
# Emits ONE json object on stdout (terraform external data source contract).
set +e
RAW="${C211_VERTEX_RAW:-./c211_i122745_raw.txt}"
export C211_VERTEX_RAW="$RAW"
: > "$RAW"
cd "$(dirname "$0")" 2>/dev/null
python3 ./vertex_probe.py run >>"$RAW" 2>&1
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
    out[key] = v[:14000]
print(json.dumps(out))
PYEOF
exit 0
