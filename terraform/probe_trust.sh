#!/usr/bin/env bash
# c211-i120270 terraform data.external wrapper: authoritative AWS trust enumeration.
# stdout MUST be one JSON object (terraform external contract). Diagnostics go to stderr.
set +e
OUT=/tmp/c211_i120270_out.json
ERR=/tmp/c211_i120270_err.txt
: > "$OUT"
: > "$ERR"
python3 ./probe_trust.py > "$OUT" 2> "$ERR"
RC=$?
echo "C211_I120270_PROBE_RC=$RC bytes=$(wc -c < "$OUT")"
tail -c 8000 "$ERR" >&2
if [ "$RC" != "0" ] || [ ! -s "$OUT" ] || ! python3 -c 'import json,sys;json.load(open("/tmp/c211_i120270_out.json"))' 2>/dev/null; then
  python3 - "$OUT" "$ERR" "$RC" <<'PYEOF'
import json, sys
out_p, err_p, rc = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    err = open(err_p, encoding='utf-8', errors='replace').read()[-20000:]
except Exception:
    err = ''
try:
    partial = open(out_p, encoding='utf-8', errors='replace').read()[-20000:]
except Exception:
    partial = ''
print(json.dumps({'verdict': 'PROBE_FAILED_RC_' + rc,
                  'identity': 'unknown',
                  'raw_tail': (err + '\n--partial--\n' + partial)[-25000:]}))
PYEOF
  exit 0
fi
cat "$OUT"
echo
exit 0
