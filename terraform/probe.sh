#!/usr/bin/env bash
# c211-i109237 phase-2 probe: correctly-formed EC2 RunInstances DryRun (explicit
# subnet + security group, CLI-independent SigV4) and, if and only if a DryRun
# returns DryRunOperation, exactly ONE minimal marker GPU whose real device
# identity is printed to the EC2 console output and which is then terminated
# through the EC2 API inside this same step (bounded waits, unconditional cleanup).
# Emits ONE json object on stdout (terraform external data source contract).
set +e
export AWS_PAGER=""
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-eu-west-2}}"
RAW="${C211_PROBE_RAW:-/tmp/c211_i109237_p2_raw.txt}"
: > "$RAW"
say() { printf '%s\n' "$*" >> "$RAW"; }
cap() { head -c "${2:-1200}"; }

say "== aws identity =="
python3 ./probe_aws2.py sts "$REGION" >> "$RAW" 2>&1
aws sts get-caller-identity --output json 2>&1 | cap 500 >> "$RAW"
say ""

say "== aws quotas eu-west-2 =="
for q in L-DB2E81BA L-3819A6DF L-417A185B; do
  aws service-quotas get-service-quota --service-code ec2 --quota-code "$q" --region "$REGION" \
    --query 'Quota.{code:QuotaCode,value:Value}' --output text 2>&1 | cap 200 >> "$RAW"
done

say "== aws network inventory =="
python3 ./probe_aws2.py inventory "$REGION" >> "$RAW" 2>&1
say "-- cli subnets"
aws ec2 describe-subnets --region "$REGION" \
  --query 'Subnets[].[SubnetId,VpcId,AvailabilityZone,DefaultForAz,State,AvailableIpAddressCount]' \
  --output text 2>&1 | cap 900 >> "$RAW"
say "-- cli sgs"
aws ec2 describe-security-groups --region "$REGION" \
  --query 'SecurityGroups[].[GroupId,GroupName,VpcId]' --output text 2>&1 | cap 900 >> "$RAW"

say "== aws corrected run-instances dry-run (explicit subnet+sg) =="
TARGET=$(grep -m1 '^SIGV4_TARGET ' "$RAW" | sed 's/^SIGV4_TARGET //')
SUBNET=$(printf '%s' "$TARGET" | python3 -c "import json,sys
try: print((json.load(sys.stdin) or {}).get('subnet',{}).get('id',''))
except Exception: print('')" 2>/dev/null)
SG=$(printf '%s' "$TARGET" | python3 -c "import json,sys
try: print(((json.load(sys.stdin) or {}).get('sg') or {}).get('id',''))
except Exception: print('')" 2>/dev/null)
say "PICKED subnet=$SUBNET sg=$SG"
AMI=ami-0885905088d594c34
DLAMI=ami-0a642be351f405d98
MARKER_TYPE=""
for t in g5.xlarge g5.12xlarge p5.4xlarge p4d.24xlarge p5.48xlarge; do
  L=$(python3 ./probe_aws2.py dryrun2 "$REGION" "$t" "$AMI" "$SUBNET" "$SG" 2>&1)
  say "$L"
  if [ -z "$MARKER_TYPE" ] && printf '%s' "$L" | grep -q 'meaning=AUTHORIZED'; then MARKER_TYPE="$t"; fi
done
say "-- cli cross-check (cli-input-json, g5.xlarge)"
aws ec2 run-instances --region "$REGION" --cli-input-json \
  "{\"DryRun\":true,\"ImageId\":\"$AMI\",\"InstanceType\":\"g5.xlarge\",\"MinCount\":1,\"MaxCount\":1,\"SubnetId\":\"$SUBNET\",\"SecurityGroupId\":[\"$SG\"]}" \
  2>&1 | cap 600 >> "$RAW"

say "== aws marker (only if a DryRun returned DryRunOperation) =="
if [ -z "$MARKER_TYPE" ]; then
  say "MARKER_SKIPPED no dry-run authorization (see verdicts above)"
else
  say "MARKER_TYPE=$MARKER_TYPE DLAMI=$DLAMI SUBNET=$SUBNET SG=$SG"
  OUT=$(python3 ./probe_aws2.py run "$REGION" "$MARKER_TYPE" "$DLAMI" "$SUBNET" "$SG" ./marker_userdata.sh c211-i109237-marker 2>&1)
  say "$OUT"
  IID=$(printf '%s' "$OUT" | sed -n 's/^C211_MARKER_INSTANCE_ID=//p' | head -1)
  if [ -z "$IID" ]; then
    say "MARKER_LAUNCH_FAILED no instance id; nothing to terminate"
  else
    say "MARKER_INSTANCE=$IID"
    for i in $(seq 1 12); do
      sleep 10
      D=$(python3 ./probe_aws2.py describe "$REGION" "$IID" 2>&1)
      say "$D"
      ST=$(printf '%s' "$D" | sed -n 's/.* state=\([a-z]*\) .*/\1/p')
      if [ "$ST" = "running" ] || [ "$ST" = "stopped" ] || [ "$ST" = "terminated" ]; then break; fi
    done
    FOUND=""
    for i in $(seq 1 10); do
      C=$(python3 ./probe_aws2.py console "$REGION" "$IID" 2>&1)
      say "$C"
      if printf '%s' "$C" | grep -q 'GPU-\|C211_MARKER_END'; then FOUND=1; break; fi
      sleep 12
    done
    say "MARKER_CONSOLE_EVIDENCE=${FOUND:-none}"
    say "== aws marker teardown (unconditional) =="
    say "$(python3 ./probe_aws2.py terminate "$REGION" "$IID" 2>&1)"
    for i in $(seq 1 8); do
      sleep 8
      D=$(python3 ./probe_aws2.py describe "$REGION" "$IID" 2>&1)
      say "$D"
      ST=$(printf '%s' "$D" | sed -n 's/.* state=\([a-z]*\) .*/\1/p')
      if [ "$ST" = "terminated" ]; then break; fi
    done
    say "MARKER_TEARDOWN_FINAL=$(python3 ./probe_aws2.py describe "$REGION" "$IID" 2>&1)"
  fi
fi

say "== gcp identity/quota re-read (no --project) =="
gcloud config list --format="value(core.project)" 2>&1 | cap 200 >> "$RAW"
for r in us-central1 europe-west4; do
  say "-- region $r"
  gcloud compute regions describe "$r" --format=json 2>/dev/null | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: print('REGION_JSON_UNAVAILABLE'); sys.exit(0)
for q in d.get('quotas',[]):
    m=q.get('metric','')
    if 'GPU' in m or 'gpu' in m:
        print('%s limit=%s usage=%s' % (m, q.get('limit'), q.get('usage')))
" >> "$RAW" 2>&1
done
say "-- aiplatform"
gcloud ai custom-jobs list --region=us-central1 --limit=3 --format="value(name)" 2>&1 | cap 400 >> "$RAW"

say "== azure re-read =="
az account show --query '{name:name,state:state}' --output tsv 2>&1 | cap 200 >> "$RAW"
for loc in uksouth westeurope; do
  say "-- $loc"
  az vm list-usage --location "$loc" \
    --query "[?contains(localName,'NC')||contains(localName,'ND')||contains(localName,'NV')||contains(localName,'GPU')].{n:localName,c:currentValue,l:limit}" \
    --output tsv 2>&1 | cap 900 >> "$RAW"
done

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
    out[key] = v[:2600]
print(json.dumps(out))
PYEOF
exit 0
