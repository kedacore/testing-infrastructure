#!/usr/bin/env bash
# c211-i121988 phase-p5x probe: prove the REAL device identity of the first
# on-demand P-family (NVIDIA H100 / A100) marker launched under the kedacore
# pull_request_target OIDC role in eu-west-2, then prove teardown/absence.
# Read-only except: exactly ONE on-demand marker instance alive at any time.
#
# Order:
#  1) sts get-caller-identity (must be assumed-role/github-role/GitHubActions)
#  2) servicequotas L-417A185B (P on-demand), L-7212CCBC (P spot), L-DB2E81BA,
#     L-3819A6DF in eu-west-2 + describe-instance-type-offerings for
#     p5.4xlarge/p4d.24xlarge in eu-west-2a
#  3) RunInstances DryRun p5.4xlarge on subnet-0e770146b6917a067 (fallback
#     subnet-042ac79772788812c), sg-012549f2bc6556c0e, NO AvailabilityZone
#  4) launch ONE nonce-tagged ON-DEMAND p5.4xlarge, console Latest=true poll for
#     the C211_MARKER / nvidia-smi -L / --query-gpu device lines
#  5) independent DescribeInstances readback, then terminate + absence readback
#  6) ONLY if p5.4xlarge is provider-verbatim refused, the identical single-marker
#     flow once with p4d.24xlarge
# Emits ONE json object on stdout (terraform external data source contract).
set +e
export AWS_PAGER=""
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-eu-west-2}}"
RAW="${C211_PROBE_RAW:-/tmp/c211_i121988_p5x_raw.txt}"
: > "$RAW"
say() { printf '%s\n' "$*" >> "$RAW"; }
cap() { head -c "${2:-1200}"; }

SUBNET=subnet-0e770146b6917a067
SUBNET2=subnet-042ac79772788812c
SG=sg-012549f2bc6556c0e
TAG=c211-i121988-p5marker

say "== aws identity =="
python3 ./probe_aws2.py sts "$REGION" >> "$RAW" 2>&1
aws sts get-caller-identity --output json 2>&1 | cap 500 >> "$RAW"

say "== servicequotas eu-west-2 (L-417A185B P on-demand, L-7212CCBC P spot, L-DB2E81BA G/VT, L-3819A6DF G/VT spot) =="
for q in L-417A185B L-7212CCBC L-DB2E81BA L-3819A6DF; do
  say "$q	$(aws service-quotas get-service-quota --service-code ec2 --quota-code "$q" --region "$REGION" \
        --query 'Quota.[QuotaName,Value]' --output text 2>&1 | cap 200)"
done

say "== describe-instance-type-offerings eu-west-2a =="
for t in p5.4xlarge p4d.24xlarge; do
  say "-- $t @ eu-west-2a"
  say "$(aws ec2 describe-instance-type-offerings --region "$REGION" --location-type availability-zone \
        --filters Name=instance-type,Values="$t" Name=location,Values=eu-west-2a \
        --query 'InstanceTypeOfferings[].[InstanceType,LocationType,Location]' --output text 2>&1 | cap 400)"
done
say "-- all p5.4xlarge/p4d.24xlarge offered AZs"
say "$(aws ec2 describe-instance-type-offerings --region "$REGION" --location-type availability-zone \
      --filters Name=instance-type,Values=p5.4xlarge,p4d.24xlarge \
      --query 'InstanceTypeOfferings[].[InstanceType,Location]' --output text 2>&1 | cap 800)"

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

say "== dry-run p5.4xlarge explicit eu-west-2a subnet + sg, no AZ =="
if [ -n "$AMI" ]; then
  say "$(python3 ./probe_aws2.py dryrun2 "$REGION" p5.4xlarge "$AMI" "$SUBNET" "$SG" 2>&1)"
  say "$(python3 ./probe_aws2.py dryrun2 "$REGION" p5.4xlarge "$AMI" "$SUBNET2" "$SG" 2>&1)"
  say "$(python3 ./probe_aws2.py dryrun2 "$REGION" p4d.24xlarge "$AMI" "$SUBNET" "$SG" 2>&1)"
fi

launch_and_prove() {
  local T="$1" S="$2"
  local OUT IID D ST C FOUND FINAL
  say "---- launch candidate type=$T subnet=$S tag=$TAG"
  OUT=$(python3 ./probe_aws2.py run2 "$REGION" "$T" "$AMI" "$S" "$SG" \
        ./marker_userdata_p5x.sh "$TAG" 2>&1)
  say "$OUT"
  IID=$(printf '%s' "$OUT" | sed -n 's/^C211_MARKER_INSTANCE_ID=//p' | head -1)
  if [ -z "$IID" ]; then
    say "LAUNCH_REFUSED type=$T subnet=$S (verbatim above)"
    echo "REFUSED"
    return 0
  fi
  say "MARKER_INSTANCE=$IID chosen=$T/$S"
  echo "$IID" > /tmp/c211_i121988_iid.txt
  say "== boot state =="
  for i in $(seq 1 12); do
    sleep 10
    D=$(python3 ./probe_aws2.py describe "$REGION" "$IID" 2>&1)
    say "$D"
    ST=$(printf '%s' "$D" | sed -n 's/.* state=\([a-z]*\) .*/\1/p')
    if [ "$ST" = "running" ] || [ "$ST" = "terminated" ] || [ "$ST" = "stopped" ]; then break; fi
  done
  say "== console poll (Latest=true, Nitro real-time) =="
  FOUND=""
  for i in $(seq 1 36); do
    C=$(python3 ./probe_aws2.py console2 "$REGION" "$IID" latest 2>&1)
    say "$C"
    if printf '%s' "$C" | grep -qE 'GPU-[0-9a-f]{8}-|C211_MARKER_END'; then FOUND=1; break; fi
    sleep 15
  done
  say "$(python3 ./probe_aws2.py screenshot "$REGION" "$IID" 2>&1)"
  say "== independent readback (DescribeInstances) before teardown =="
  say "$(python3 ./probe_aws2.py describe "$REGION" "$IID" 2>&1)"
  say "== marker terminate (unconditional) + absence readback =="
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
  say "P5X_FINAL_STATE=${FINAL:-none}"
  say "P5X_ABSENT_READBACK=$(python3 ./probe_aws2.py describe "$REGION" "$IID" 2>&1)"
  echo "LAUNCHED"
}

LAUNCHED=""
REFUSED_P5=""
if [ -z "$AMI" ]; then
  say "AMI_UNRESOLVED no launch attempted"
else
  for cand in "p5.4xlarge:$SUBNET" "p5.4xlarge:$SUBNET2"; do
    T="${cand%%:*}"; S="${cand##*:}"
    R=$(launch_and_prove "$T" "$S")
    if [ "$R" = "LAUNCHED" ]; then LAUNCHED=1; break; fi
    REFUSED_P5=1
  done
  if [ -z "$LAUNCHED" ] && [ -n "$REFUSED_P5" ]; then
    say "== p5.4xlarge provider-verbatim refused on both subnets; ONE p4d.24xlarge fallback attempt =="
    R=$(launch_and_prove p4d.24xlarge "$SUBNET")
    [ "$R" = "LAUNCHED" ] && LAUNCHED=1
  fi
fi

say "== aggregated marker list (this lane's tag) =="
say "$(python3 ./probe_aws2.py list "$REGION" "$TAG" 2>&1)"
say "== every non-terminated instance in the region (account-wide) =="
say "$(python3 ./probe_aws2.py live "$REGION" 2>&1)"

UUID=$(grep -oE 'GPU-[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}' "$RAW" 2>/dev/null | head -1)
SKU=$(grep -oE 'p5\.4xlarge|p4d\.24xlarge' "$RAW" 2>/dev/null | head -1)
VERDICT="NO_UUID"
[ -n "$UUID" ] && VERDICT="READY_DEVICE_UUID_PROVEN"
say "C211_I121988_VERDICT=$VERDICT"
say "C211_I121988_GPU_UUID=${UUID:-none}"
say "C211_I121988_SKU=${SKU:-none}"

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
for line in raw.splitlines():
    for tag in ('C211_I121988_VERDICT=', 'C211_I121988_GPU_UUID=', 'C211_I121988_SKU='):
        if line.startswith(tag):
            out[tag.rstrip('=')] = line.split('=', 1)[1]
out['raw_tail'] = raw[-6000:]
print(json.dumps(out))
PYEOF
exit 0
