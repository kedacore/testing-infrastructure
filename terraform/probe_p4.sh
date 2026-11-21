#!/usr/bin/env bash
# c211-i119806 phase-4 probe: launch EXACTLY ONE on-demand g5.xlarge marker GPU
# in eu-west-2a (no AvailabilityZone parameter; explicit eu-west-2a subnet from
# the proven 412 DryRunOperation set), resolve a GPU-capable AMI, print the real
# device identity (nvidia-smi -L UUID) from the EC2 console output, terminate it
# through TerminateInstances and prove absence by readback.
# Emits ONE json object on stdout (terraform external data source contract).
set +e
export AWS_PAGER=""
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-eu-west-2}}"
RAW="${C211_PROBE_RAW:-/tmp/c211_i119806_p4_raw.txt}"
: > "$RAW"
say() { printf '%s\n' "$*" >> "$RAW"; }
cap() { head -c "${2:-1200}"; }

say "== aws identity =="
python3 ./probe_aws2.py sts "$REGION" >> "$RAW" 2>&1
aws sts get-caller-identity --output json 2>&1 | cap 500 >> "$RAW"

say "== aws quota re-read (eu-west-2 G/VT On-Demand L-DB2E81BA) =="
for q in L-DB2E81BA L-3819A6DF; do
  aws service-quotas get-service-quota --service-code ec2 --quota-code "$q" --region "$REGION" \
    --query 'Quota.{code:QuotaCode,value:Value}' --output text 2>&1 | cap 200 >> "$RAW"
done

say "== ami resolution (GPU-capable image proof) =="
AMI=""
for p in \
  /aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id \
  /aws/service/deeplearning/ami/x86_64/oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id \
  /aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-amazon-linux-2023/latest/ami-id ; do
  A=$(aws ssm get-parameter --name "$p" --region "$REGION" --query Parameter.Value --output text 2>/dev/null)
  say "ssm $p -> ${A:-none}"
  case "$A" in ami-*) AMI="$A"; break;; esac
done
if [ -z "$AMI" ]; then
  A=$(aws ec2 describe-images --region "$REGION" --owners amazon \
        --filters 'Name=name,Values=Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 22.04)*' \
                  'Name=state,Values=available' \
        --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text 2>/dev/null)
  say "describe-images filter -> ${A:-none}"
  case "$A" in ami-*) AMI="$A";; esac
fi
[ -z "$AMI" ] && AMI=ami-0a642be351f405d98 && say "fallback hardcoded ami-0a642be351f405d98"
say "AMI_CHOSEN=$AMI"
say "$(aws ec2 describe-images --region "$REGION" --image-ids "$AMI" \
      --query 'Images[].[ImageId,Name,Description,Architecture,RootDeviceType,State]' --output text 2>&1 | cap 600)"
say "-- gpu image name sanity (owned-by-amazon DL/vendor images carrying GPU)"
say "$(aws ec2 describe-images --region "$REGION" --image-ids "$AMI" \
      --query 'Images[].[ImageId,OwnerId,PlatformDetails]' --output text 2>&1 | cap 300)"

say "== aws dry-run (explicit eu-west-2a subnet + sg, no AZ) =="
for pair in "g5.xlarge:subnet-0e770146b6917a067" "g5.xlarge:subnet-042ac79772788812c"; do
  T="${pair%%:*}"; S="${pair##*:}"
  say "$(python3 ./probe_aws2.py dryrun2 "$REGION" "$T" "$AMI" "$S" sg-012549f2bc6556c0e 2>&1)"
done

say "== aws marker launch (candidate loop, ONE instance at a time) =="
IID=""
CHOSEN=""
for cand in "g5.xlarge:subnet-0e770146b6917a067:$AMI" \
            "g5.xlarge:subnet-042ac79772788812c:$AMI" \
            "g5.12xlarge:subnet-0e770146b6917a067:$AMI" \
            "p5.4xlarge:subnet-0e770146b6917a067:$AMI" \
            "g5.xlarge:subnet-0e770146b6917a067:ami-0a642be351f405d98"; do
  T=$(printf '%s' "$cand" | cut -d: -f1)
  S=$(printf '%s' "$cand" | cut -d: -f2)
  A=$(printf '%s' "$cand" | cut -d: -f3)
  say "---- candidate type=$T subnet=$S ami=$A"
  OUT=$(python3 ./probe_aws2.py run "$REGION" "$T" "$A" "$S" sg-012549f2bc6556c0e ./marker_userdata.sh c211-i119806-marker 2>&1)
  say "$OUT"
  IID=$(printf '%s' "$OUT" | sed -n 's/^C211_MARKER_INSTANCE_ID=//p' | head -1)
  if [ -n "$IID" ]; then CHOSEN="$T/$S/$A"; say "MARKER_INSTANCE=$IID chosen=$CHOSEN"; break; fi
  say "candidate refused (see code above); trying next"
done

say "== aws marker boot + console device proof =="
if [ -z "$IID" ]; then
  say "MARKER_NOT_LAUNCHED all candidates refused; nothing exists and nothing to terminate"
else
  for i in $(seq 1 24); do
    sleep 10
    D=$(python3 ./probe_aws2.py describe "$REGION" "$IID" 2>&1)
    say "$D"
    ST=$(printf '%s' "$D" | sed -n 's/.* state=\([a-z]*\) .*/\1/p')
    if [ "$ST" = "running" ] || [ "$ST" = "terminated" ] || [ "$ST" = "stopped" ]; then break; fi
  done
  FOUND=""
  for i in $(seq 1 18); do
    C=$(python3 ./probe_aws2.py console "$REGION" "$IID" 2>&1)
    say "$C"
    if printf '%s' "$C" | grep -q 'GPU-\|C211_MARKER_END'; then FOUND=1; break; fi
    sleep 12
  done
  say "MARKER_CONSOLE_EVIDENCE=${FOUND:-none}"
  U=$(sed -n 's/.*GPU-\([0-9a-f-]*\).*/GPU-\1/p' "$RAW" | head -1)
  say "MARKER_UUID=${U:-none}"
  say "== marker terminate (unconditional) =="
  say "$(python3 ./probe_aws2.py terminate "$REGION" "$IID" 2>&1)"
  FINAL=""
  for i in $(seq 1 12); do
    sleep 8
    D=$(python3 ./probe_aws2.py describe "$REGION" "$IID" 2>&1)
    say "$D"
    ST=$(printf '%s' "$D" | sed -n 's/.* state=\([a-z]*\) .*/\1/p')
    FINAL="$ST"
    if [ "$ST" = "terminated" ]; then break; fi
  done
  say "MARKER_TEARDOWN_FINAL_STATE=$FINAL"
  say "MARKER_ABSENT_READBACK=$(python3 ./probe_aws2.py describe "$REGION" "$IID" 2>&1)"
fi

say "== aws post-run marker absence sweep (tag filter) =="
aws ec2 describe-instances --region "$REGION" \
  --filters "Name=tag:Name,Values=c211-i119806-marker" \
  --query 'Reservations[].Instances[].[InstanceId,State.Name,InstanceType]' --output text 2>&1 | cap 800 >> "$RAW"
say "-- any non-terminated c211-i119806 marker:"
aws ec2 describe-instances --region "$REGION" \
  --filters "Name=tag:Name,Values=c211-i119806-marker" "Name=instance-state-name,Values=pending,running,stopping,stopped" \
  --query 'length(Reservations[].Instances[])' --output text 2>&1 | cap 200 >> "$RAW"

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
out['raw_tail'] = raw[-6000:]
print(json.dumps(out))
PYEOF
exit 0
