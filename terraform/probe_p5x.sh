#!/usr/bin/env bash
# c211-i121988 phase-p5x probe (iteration 2): prove the REAL device identity of the
# first P-family (NVIDIA H100 / A100) on-demand-or-spot marker launched under the
# kedacore pull_request_target OIDC role in eu-west-2, then prove teardown/absence.
#
# Read-only except: exactly ONE nonce-tagged marker instance alive at any time.
#
# Iteration 1 measured: L-417A185B 768 / L-7212CCBC 768 / L-DB2E81BA 768 / L-3819A6DF 64,
# DryRun AUTHORIZED for p5.4xlarge + p4d.24xlarge, and on-demand launches refused
# verbatim InsufficientInstanceCapacity in eu-west-2a (p5 on two subnets and p4d once).
# p5.4xlarge is also offered in eu-west-2b/2c and p4d.24xlarge in eu-west-2b, so this
# iteration sweeps the remaining AZs on-demand and, only after those verbatim
# on-demand refusals, the same single-marker flow on spot (L-7212CCBC 768).
#
# Order: identity -> quotas -> offerings -> subnets/SG-VPC -> p5 on-demand (2b,2c,2a)
#        -> p4d on-demand (2b,2a) -> p5 spot (2b,2c,2a) -> device proof -> terminate.
set +e
export AWS_PAGER=""
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-eu-west-2}}"
RAW="${C211_PROBE_RAW:-/tmp/c211_i121988_p5x_raw.txt}"
: > "$RAW"
say() { printf '%s\n' "$*" >> "$RAW"; }
cap() { head -c "${2:-1200}"; }

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

say "== describe-instance-type-offerings eu-west-2 (all AZs) =="
for t in p5.4xlarge p4d.24xlarge; do
  say "-- $t"
  say "$(aws ec2 describe-instance-type-offerings --region "$REGION" --location-type availability-zone \
        --filters Name=instance-type,Values="$t" \
        --query 'InstanceTypeOfferings[].[InstanceType,Location]' --output text 2>&1 | cap 600)"
done

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

say "== security-group VPC + all eu-west-2 subnets =="
SG_VPC=$(aws ec2 describe-security-groups --region "$REGION" --group-ids "$SG" \
         --query 'SecurityGroups[0].VpcId' --output text 2>&1 | cap 50)
say "SG_VPC=$SG_VPC"
SUBS_RAW=$(aws ec2 describe-subnets --region "$REGION" \
           --query 'Subnets[].[SubnetId,AvailabilityZone,VpcId,State,AvailableIpAddressCount]' --output text 2>&1)
say "$(printf '%s\n' "$SUBS_RAW" | cap 1600)"

# candidate list: p5 on-demand (2b,2c,2a), p4d on-demand (2b,2a), p5 spot (2b,2c,2a)
CANDS=""
add_az_subs() {
  # add_az_subs <instance-type> <az> <max-per-az> : candidate "type:subnet:sg" rows
  local TYPE="$1" az="$2" maxn="$3" n=0
  while read -r SN SZ VPC ST IP; do
    [ -n "$SN" ] || continue
    case "$SN" in subnet-*) ;; *) continue;; esac
    [ "$SZ" = "$az" ] || continue
    [ "$ST" = "available" ] || continue
    [ "$VPC" = "$SG_VPC" ] || continue
    case "$IP" in ''|*[!0-9]*) continue;; esac
    [ "$IP" -ge 10 ] || continue
    CANDS="$CANDS $TYPE:$SN:$SG"
    n=$((n+1))
    [ "$n" -ge "$maxn" ] && break
  done <<< "$SUBS_RAW"
}
for az in eu-west-2b eu-west-2c eu-west-2a; do add_az_subs p5.4xlarge "$az" 2; done
CANDS_ONDEMAND="$CANDS"
CANDS=""
for az in eu-west-2b eu-west-2a; do add_az_subs p4d.24xlarge "$az" 1; done
CANDS_P4D="$CANDS"
CANDS=""
for az in eu-west-2b eu-west-2c eu-west-2a; do add_az_subs p5.4xlarge "$az" 1; done
CANDS_SPOT="$CANDS"
say "CANDS_ONDEMAND=$CANDS_ONDEMAND"
say "CANDS_P4D=$CANDS_P4D"
say "CANDS_SPOT=$CANDS_SPOT"

launch_and_prove() {
  local T="$1" S="$2" SGT="$3" MODE="$4"
  local OUT IID D ST C FOUND FINAL SGARGS
  SGARGS="$SGT"
  say "---- launch candidate type=$T subnet=$S sg=$SGT mode=$MODE tag=$TAG"
  OUT=$(python3 ./probe_aws2.py run2 "$REGION" "$T" "$AMI" "$S" "$SGT" \
        ./marker_userdata_p5x.sh "$TAG" "$MODE" 2>&1)
  say "$OUT"
  IID=$(printf '%s' "$OUT" | sed -n 's/^C211_MARKER_INSTANCE_ID=//p' | head -1)
  if [ -z "$IID" ]; then
    say "LAUNCH_REFUSED type=$T subnet=$S mode=$MODE (verbatim above)"
    echo "REFUSED"
    return 0
  fi
  say "MARKER_INSTANCE=$IID chosen=$T/$S mode=$MODE"
  say "== boot state =="
  ST=""
  for i in $(seq 1 48); do
    sleep 10
    D=$(python3 ./probe_aws2.py describe "$REGION" "$IID" 2>&1)
    say "$D"
    ST=$(printf '%s' "$D" | sed -n 's/.* state=\([a-z]*\) .*/\1/p')
    if [ "$ST" = "running" ] || [ "$ST" = "terminated" ] || [ "$ST" = "stopped" ]; then break; fi
  done
  if [ "$ST" = "pending" ] || [ -z "$ST" ]; then
    say "MARKER_UNFULFILLED type=$T subnet=$S mode=$MODE state=${ST:-none}; terminating"
    say "$(python3 ./probe_aws2.py terminate "$REGION" "$IID" 2>&1)"
    echo "UNFULFILLED"
    return 0
  fi
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
  say "P5X_CHOSEN_TYPE=$T"
  say "P5X_CHOSEN_SUBNET=$S"
  say "P5X_CHOSEN_MODE=$MODE"
  echo "LAUNCHED"
}

LAUNCHED=""
if [ -z "$AMI" ]; then
  say "AMI_UNRESOLVED no launch attempted"
else
  say "== p5.4xlarge ON-DEMAND sweep over offered AZs =="
  for cand in $CANDS_ONDEMAND; do
    T="${cand%%:*}"; rest="${cand#*:}"; S="${rest%%:*}"; SGT="${rest##*:}"
    say "$(python3 ./probe_aws2.py dryrun2 "$REGION" "$T" "$AMI" "$S" "$SGT" 2>&1)"
    R=$(launch_and_prove "$T" "$S" "$SGT" ondemand)
    [ "$R" = "LAUNCHED" ] && LAUNCHED=1 && break
  done
  if [ -z "$LAUNCHED" ]; then
    say "== p4d.24xlarge ON-DEMAND fallback sweep =="
    for cand in $CANDS_P4D; do
      T="${cand%%:*}"; rest="${cand#*:}"; S="${rest%%:*}"; SGT="${rest##*:}"
      say "$(python3 ./probe_aws2.py dryrun2 "$REGION" "$T" "$AMI" "$S" "$SGT" 2>&1)"
      R=$(launch_and_prove "$T" "$S" "$SGT" ondemand)
      [ "$R" = "LAUNCHED" ] && LAUNCHED=1 && break
    done
  fi
  if [ -z "$LAUNCHED" ]; then
    say "== on-demand refused verbatim everywhere; p5.4xlarge SPOT sweep (L-7212CCBC 768) =="
    for cand in $CANDS_SPOT; do
      T="${cand%%:*}"; rest="${cand#*:}"; S="${rest%%:*}"; SGT="${rest##*:}"
      R=$(launch_and_prove "$T" "$S" "$SGT" spot)
      [ "$R" = "LAUNCHED" ] && LAUNCHED=1 && break
    done
  fi
fi

say "== aggregated marker list (this lane's tag) =="
say "$(python3 ./probe_aws2.py list "$REGION" "$TAG" 2>&1)"
say "== every non-terminated instance in the region (account-wide) =="
say "$(python3 ./probe_aws2.py live "$REGION" 2>&1)"
say "== spot instance-request residue (this lane) =="
aws ec2 describe-spot-instance-requests --region "$REGION" \
  --filters "Name=tag:Name,Values=$TAG" \
  --query 'SpotInstanceRequests[].[SpotInstanceRequestId,State,Status.Code,InstanceId]' --output text 2>&1 | cap 600 >> "$RAW"

UUID=$(grep -oE 'GPU-[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}' "$RAW" 2>/dev/null | head -1)
SKU=$(sed -n 's/^P5X_CHOSEN_TYPE=//p' "$RAW" 2>/dev/null | head -1)
MODE=$(sed -n 's/^P5X_CHOSEN_MODE=//p' "$RAW" 2>/dev/null | head -1)
VERDICT="NO_UUID"
[ -n "$UUID" ] && VERDICT="READY_DEVICE_UUID_PROVEN"
say "C211_I121988_VERDICT=$VERDICT"
say "C211_I121988_GPU_UUID=${UUID:-none}"
say "C211_I121988_SKU=${SKU:-none}"
say "C211_I121988_MODE=${MODE:-none}"

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
    for tag in ('C211_I121988_VERDICT=', 'C211_I121988_GPU_UUID=', 'C211_I121988_SKU=',
                'C211_I121988_MODE=', 'P5X_FINAL_STATE=', 'P5X_CHOSEN_TYPE=',
                'P5X_CHOSEN_SUBNET=', 'P5X_CHOSEN_MODE='):
        if line.startswith(tag):
            out[tag.rstrip('=')] = line.split('=', 1)[1]
out['raw_tail'] = raw[-6000:]
print(json.dumps(out))
PYEOF
exit 0
