#!/usr/bin/env bash
# c211-i109237 read-only multi-cloud identity + GPU launch-authorization probe.
# Creates no resources. Prints identifiers and quota/verdict values only (no secret material).
# Emits ONE json object on stdout (terraform external data source contract).
set +e
export AWS_PAGER=""
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-eu-west-2}}"
RAW="${C211_PROBE_RAW:-/tmp/c211_i109237_probe_raw.txt}"
: > "$RAW"
say() { printf '%s\n' "$*" >> "$RAW"; }
cap() { head -c "${2:-1200}"; }

say "== env presence (names only) =="
for v in ARM_CLIENT_ID ARM_TENANT_ID ARM_SUBSCRIPTION_ID ARM_USE_OIDC AWS_ROLE_ARN AWS_REGION \
         AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN \
         GOOGLE_APPLICATION_CREDENTIALS CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE \
         CLOUDSDK_CORE_PROJECT GCLOUD_PROJECT GOOGLE_CLOUD_PROJECT; do
  if [ -n "${!v}" ]; then say "$v=SET"; else say "$v=UNSET"; fi
done

say "== aws cli fingerprint =="
say "which=$(command -v aws 2>&1)"
say "version=$(aws --version 2>&1)"
say "binary_head=$(head -c 120 "$(command -v aws 2>/dev/null)" 2>/dev/null | tr -d '\r' | head -3)"
say "run-instances-help=$(aws ec2 run-instances help 2>&1 | head -c 300 | tr '\n' ' ')"

say "== aws region =="
say "$REGION"

say "== aws sts get-caller-identity =="
aws sts get-caller-identity --output json 2>&1 | cap 900 >> "$RAW"
say ""
say "== sigv4 sts control =="
python3 ./probe_aws.py sts >> "$RAW" 2>&1

say "== aws service quotas (eu-west-2) =="
for q in L-DB2E81BA L-3819A6DF L-417A185B; do
  say "-- $q"
  aws service-quotas get-service-quota --service-code ec2 --quota-code "$q" --region "$REGION" \
    --query 'Quota.{code:QuotaCode,name:QuotaName,value:Value,adjustable:Adjustable}' --output json 2>&1 | cap 600 >> "$RAW"
  say ""
done

say "== aws instance type offerings (eu-west-2) =="
aws ec2 describe-instance-type-offerings --region "$REGION" --location-type region \
  --filters "Name=instance-type,Values=g6e.xlarge,g6e.2xlarge,g6e.12xlarge,g6e.48xlarge,g5.xlarge,g5.12xlarge,p3.2xlarge,p4d.24xlarge,p5.48xlarge,p5.4xlarge" \
  --query 'InstanceTypeOfferings[].InstanceType' --output text 2>&1 | cap 900 >> "$RAW"
say ""
say "-- sigv4 offerings"
python3 ./probe_aws.py offerings "$REGION" g6e.xlarge,g6e.2xlarge,g6e.12xlarge,g6e.48xlarge,g5.xlarge,g5.12xlarge,p4d.24xlarge,p5.48xlarge >> "$RAW" 2>&1

say "== aws network inventory read-only =="
python3 ./probe_aws.py vpcs "$REGION" >> "$RAW" 2>&1
python3 ./probe_aws.py subnets "$REGION" >> "$RAW" 2>&1
python3 ./probe_aws.py sgs "$REGION" >> "$RAW" 2>&1

say "== aws AMIs (eu-west-2) =="
AMI=$(aws ec2 describe-images --region "$REGION" --owners 099720109477 \
  --filters "Name=name,Values=ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*" "Name=state,Values=available" \
  --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text 2>/dev/null)
say "UBUNTU_AMI=$AMI"
DLAMI=$(aws ec2 describe-images --region "$REGION" --owners 898082745236 \
  --filters "Name=name,Values=Deep Learning OSS Nvidia Driver AMI*" "Name=state,Values=available" \
  --query 'sort_by(Images,&CreationDate)[-1].[ImageId,Name]' --output text 2>/dev/null)
say "DLAMI=$DLAMI"
AL2023GPU=$(aws ec2 describe-images --region "$REGION" --owners amazon \
  --filters "Name=name,Values=al2023-ami-2023.*-kernel-*-x86_64" "Name=state,Values=available" \
  --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text 2>/dev/null)
say "AL2023_AMI=$AL2023GPU"

say "== aws run-instances dry-run oracle (CLI) =="
for t in g6e.2xlarge p4d.24xlarge p5.48xlarge; do
  say "---- type=$t shape=A (--dry-run, no --output)"
  aws ec2 run-instances --dry-run --region "$REGION" --image-id "$AMI" \
    --instance-type "$t" --min-count 1 --max-count 1 2>&1 | cap 900 >> "$RAW"
  say ""
done
say "---- shape=B (--output json, flag last) type=p4d.24xlarge"
aws ec2 run-instances --region "$REGION" --image-id "$AMI" --instance-type p4d.24xlarge \
  --min-count 1 --max-count 1 --dry-run --output json 2>&1 | cap 900 >> "$RAW"
say ""
say "---- shape=C (--cli-input-json) type=p5.48xlarge"
aws ec2 run-instances --region "$REGION" --cli-input-json \
  "{\"DryRun\":true,\"ImageId\":\"$AMI\",\"InstanceType\":\"p5.48xlarge\",\"MinCount\":1,\"MaxCount\":1}" \
  2>&1 | cap 900 >> "$RAW"

say "== aws run-instances dry-run oracle (sigv4, CLI-independent) =="
for t in g6e.2xlarge p4d.24xlarge p5.48xlarge; do
  python3 ./probe_aws.py dryrun "$REGION" "$t" "$AMI" >> "$RAW" 2>&1
done

say "== gcp identity (no --project: relies on config core project) =="
GCP_SA=""
if [ -n "$GOOGLE_APPLICATION_CREDENTIALS" ] && [ -f "$GOOGLE_APPLICATION_CREDENTIALS" ]; then
  GCP_SA=$(python3 -c "import json,sys;d=json.load(open(sys.argv[1]));print((d.get('client_email') or '')+'|'+(d.get('project_id') or ''))" "$GOOGLE_APPLICATION_CREDENTIALS" 2>/dev/null)
fi
say "GCP_CRED_FILE_SA_AND_PROJECT=$GCP_SA"
gcloud auth list --format="value(account)" 2>&1 | cap 400 >> "$RAW"
say ""
gcloud config list 2>&1 | cap 700 >> "$RAW"
say ""

say "== gcp enabled services (no --project) =="
gcloud services list --enabled --limit=60 --format="value(config.name)" 2>&1 | cap 1800 >> "$RAW"

for r in us-central1 europe-west4; do
  say "== gcp region $r GPU quotas (no --project) =="
  gcloud compute regions describe "$r" --format=json 2>"${C211_PROBE_TMP:-/tmp}/c211_i109237_gcp_err.txt" | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
except Exception:
    print('REGION_JSON_UNAVAILABLE'); sys.exit(0)
print('REGION=%s status=%s' % (d.get('name'), d.get('status')))
n=0
for q in d.get('quotas',[]):
    m=q.get('metric','')
    if 'GPU' in m or 'gpu' in m:
        n+=1
        print('%s limit=%s usage=%s' % (m, q.get('limit'), q.get('usage')))
if n==0: print('NO_GPU_QUOTA_BUCKETS')
" >> "$RAW" 2>&1
  say "-- stderr: $(cap 200 < "${C211_PROBE_TMP:-/tmp}/c211_i109237_gcp_err.txt" | tr '\n' ' ')"
done

say "== gcp aiplatform + compute inventory (no --project) =="
gcloud ai custom-jobs list --region=us-central1 --limit=5 --format="value(name,state)" 2>&1 | cap 700 >> "$RAW"
say "-- endpoints"
gcloud ai endpoints list --region=us-central1 --limit=5 --format="value(name)" 2>&1 | cap 700 >> "$RAW"
say "-- compute instances"
gcloud compute instances list --limit=10 --format="value(name,zone,status,machineType)" 2>&1 | cap 900 >> "$RAW"

say "== azure account (identifiers only) =="
az account show --query '{name:name,id:id,tenantId:tenantId,user:user.name,state:state}' --output json 2>&1 | cap 600 >> "$RAW"
say ""
for loc in uksouth westeurope; do
  say "== az vm list-usage $loc (NC/ND/NV/GPU) =="
  az vm list-usage --location "$loc" \
    --query "[?contains(localName,'NC')||contains(localName,'ND')||contains(localName,'NV')||contains(localName,'GPU')].{name:localName,current:currentValue,limit:limit}" \
    --output json 2>&1 | cap 1400 >> "$RAW"
  say ""
done
say "== az vm/resource inventory counts (read-only) =="
az vm list -d --query 'length(@)' --output tsv 2>&1 | cap 200 >> "$RAW"
say ""
az resource list --query 'length(@)' --output tsv 2>&1 | cap 200 >> "$RAW"

# Emit the required JSON string-map on stdout only.
python3 - "$RAW" <<'PYEOF'
import json, sys
raw = open(sys.argv[1], encoding='utf-8', errors='replace').read()
sections, cur, buf = {}, 'header', []
for line in raw.splitlines():
    if line.startswith('== ') and line.rstrip().endswith(' =='):
        if buf:
            sections[cur] = '\n'.join(buf)
            buf = []
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
        seen[key] += 1
        key = '%s#%d' % (key[:60], seen[key])
    else:
        seen[key] = 0
    out[key] = v[:2500]
print(json.dumps(out))
PYEOF
exit 0
