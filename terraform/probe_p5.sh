#!/usr/bin/env bash
# c211-i119935 phase-5 probe: prove the REAL GPU device identity of an on-demand
# g5.xlarge launched under the kedacore pull_request_target OIDC role, then prove
# teardown/absence. Read-only except: exactly ONE on-demand marker instance.
#
# Strategy:
#  1) Read back the phase-4 marker i-0be5371291f2c8d13 (launched by run
#     44850618503) with GetConsoleOutput Latest=true (Nitro real-time output) --
#     the phase-4 probe used the buffered output and saw 0 bytes.
#  2) If that yields no GPU UUID, launch ONE fresh g5.xlarge in eu-west-2a
#     (explicit SubnetId, SG, NO AvailabilityZone), with user-data that waits for
#     the driver, prints nvidia-smi -L, and self-terminates.
#  3) Poll GetConsoleOutput Latest=true until the marker block appears.
#  4) Terminate unconditionally; poll DescribeInstances to terminated; aggregated
#     list of every c211 marker + every non-terminated instance in the region.
# Emits ONE json object on stdout (terraform external data source contract).
set +e
export AWS_PAGER=""
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-eu-west-2}}"
RAW="${C211_PROBE_RAW:-/tmp/c211_i119935_p5_raw.txt}"
: > "$RAW"
say() { printf '%s\n' "$*" >> "$RAW"; }
cap() { head -c "${2:-1200}"; }

PRIOR=i-0be5371291f2c8d13
SUBNET=subnet-0e770146b6917a067
SG=sg-012549f2bc6556c0e

say "== aws identity =="
python3 ./probe_aws2.py sts "$REGION" >> "$RAW" 2>&1
aws sts get-caller-identity --output json 2>&1 | cap 500 >> "$RAW"

say "== aws quota re-read eu-west-2 (L-DB2E81BA G_VT on-demand, L-3819A6DF spot) =="
for q in L-DB2E81BA L-3819A6DF; do
  aws service-quotas get-service-quota --service-code ec2 --quota-code "$q" --region "$REGION" \
    --query 'Quota.{code:QuotaCode,value:Value}' --output text 2>&1 | cap 200 >> "$RAW"
done

say "== PRIOR p4 marker readback (i-0be5371291f2c8d13, launched by run 44850618503) =="
say "$(python3 ./probe_aws2.py describe "$REGION" "$PRIOR" 2>&1)"
say "$(python3 ./probe_aws2.py console2 "$REGION" "$PRIOR" latest 2>&1)"
say "$(python3 ./probe_aws2.py console2 "$REGION" "$PRIOR" default 2>&1)"
PRIOR_UUID=$(grep -oE 'GPU-[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}' "$RAW" 2>/dev/null | head -1)
say "PRIOR_UUID=${PRIOR_UUID:-none}"

say "== ami resolution (GPU-capable image proof) =="
AMI=""
for p in \
  /aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id \
  /aws/service/deeplearning/ami/x86_64/oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id ; do
  A=$(aws ssm get-parameter --name "$p" --region "$REGION" --query Parameter.Value --output text 2>/dev/null)
  say "ssm $p -> ${A:-none}"
  case "$A" in ami-*) AMI="$A"; break;; esac
done
say "AMI_CHOSEN=${AMI:-none}"
[ -n "$AMI" ] && say "$(aws ec2 describe-images --region "$REGION" --image-ids "$AMI" \
      --query 'Images[].[ImageId,Name,Architecture,State]' --output text 2>&1 | cap 400)"

say "== dry-run g5.xlarge explicit eu-west-2a subnet + sg, no AZ =="
if [ -n "$AMI" ]; then
  say "$(python3 ./probe_aws2.py dryrun2 "$REGION" g5.xlarge "$AMI" "$SUBNET" "$SG" 2>&1)"
fi

IID=""
if [ -n "$PRIOR_UUID" ]; then
  say "== p5 marker launch SKIPPED: prior marker already yielded the real GPU UUID =="
else
  say "== p5 marker launch (exactly ONE, on-demand g5.xlarge) =="
  OUT=$(python3 ./probe_aws2.py run2 "$REGION" g5.xlarge "$AMI" "$SUBNET" "$SG" \
        ./marker_userdata_p5.sh c211-i119935-marker 2>&1)
  say "$OUT"
  IID=$(printf '%s' "$OUT" | sed -n 's/^C211_MARKER_INSTANCE_ID=//p' | head -1)

  say "== p5 boot state =="
  if [ -z "$IID" ]; then
    say "MARKER_NOT_LAUNCHED no instance id returned"
  else
    for i in $(seq 1 12); do
      sleep 10
      D=$(python3 ./probe_aws2.py describe "$REGION" "$IID" 2>&1)
      say "$D"
      ST=$(printf '%s' "$D" | sed -n 's/.* state=\([a-z]*\) .*/\1/p')
      if [ "$ST" = "running" ] || [ "$ST" = "terminated" ] || [ "$ST" = "stopped" ]; then break; fi
    done
    say "== p5 console poll (Latest=true, Nitro real-time) =="
    for i in $(seq 1 40); do
      C=$(python3 ./probe_aws2.py console2 "$REGION" "$IID" latest 2>&1)
      say "$C"
      if printf '%s' "$C" | grep -qE 'GPU-[0-9a-f]{8}-|C211_MARKER_END'; then break; fi
      sleep 20
    done
    say "$(python3 ./probe_aws2.py screenshot "$REGION" "$IID" 2>&1)"
    say "== p5 terminate (unconditional) + absence readback =="
    say "$(python3 ./probe_aws2.py terminate "$REGION" "$IID" 2>&1)"
    FINAL=""
    for i in $(seq 1 24); do
      sleep 8
      D=$(python3 ./probe_aws2.py describe "$REGION" "$IID" 2>&1)
      say "$D"
      ST=$(printf '%s' "$D" | sed -n 's/.* state=\([a-z]*\) .*/\1/p')
      FINAL="$ST"
      if [ "$ST" = "terminated" ]; then break; fi
    done
    say "P5_FINAL_STATE=${FINAL:-none}"
    say "P5_ABSENT_READBACK=$(python3 ./probe_aws2.py describe "$REGION" "$IID" 2>&1)"
  fi
fi

say "== aggregated marker list (both lanes' tags) =="
say "$(python3 ./probe_aws2.py list "$REGION" 2>&1)"
say "== every non-terminated instance in the region (account-wide) =="
say "$(python3 ./probe_aws2.py live "$REGION" 2>&1)"

UUID=$(grep -oE 'GPU-[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}' "$RAW" 2>/dev/null | head -1)
VERDICT="NO_UUID"
[ -n "$UUID" ] && VERDICT="READY_DEVICE_UUID_PROVEN"
say "C211_I119935_VERDICT=$VERDICT"
say "C211_I119935_GPU_UUID=${UUID:-none}"
say "C211_I119935_PRIOR_INSTANCE=$PRIOR"
say "C211_I119935_NEW_INSTANCE=${IID:-none}"

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
    out[key] = v[:4000]
out['raw_tail'] = raw[-8000:]
if len(json.dumps(out)) > 300000:
    out['raw_tail'] = raw[-3000:]
print(json.dumps(out))
PYEOF
exit 0
# retrigger i119935 2026-11-21T01:56:48Z (comment only; no behaviour change)
