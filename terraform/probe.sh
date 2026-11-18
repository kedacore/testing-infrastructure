#!/usr/bin/env bash
# c211-i108923 read-only cloud identity + GPU-quota probe.
# Creates no resources and prints no secret values (identifiers only).
set +e
export AWS_PAGER=""
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-eu-west-2}}"
RAW=/tmp/c211_i108923_probe_raw.txt
: > "$RAW"
say() { printf '%s\n' "$*" >> "$RAW"; }

say "== env presence (names only) =="
for v in ARM_CLIENT_ID ARM_TENANT_ID ARM_SUBSCRIPTION_ID ARM_USE_OIDC AWS_ROLE_ARN AWS_REGION AWS_ACCESS_KEY_ID GOOGLE_APPLICATION_CREDENTIALS CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE; do
  if [ -n "${!v}" ]; then say "$v=SET"; else say "$v=UNSET"; fi
done

say "== aws region =="
say "$REGION"

say "== aws sts get-caller-identity =="
aws sts get-caller-identity --output json >> "$RAW" 2>&1

for q in L-DB2E81BA L-3819A6DF L-417A185B; do
  say "== aws service-quota $q =="
  aws service-quotas get-service-quota --service-code ec2 --quota-code "$q" --region "$REGION" --output json >> "$RAW" 2>&1
done

say "== aws ec2 NVIDIA instance types ($REGION) =="
aws ec2 describe-instance-types --region "$REGION" \
  --filters "Name=GpuInfo.Gpus.Manufacturer,Values=NVIDIA" \
  --query 'InstanceTypes[].[InstanceType,GpuInfo.Gpus[0].Name,GpuInfo.Gpus[0].Count,GpuInfo.TotalGpuMemoryInMiB]' \
  --output text 2>&1 | head -40 >> "$RAW"

say "== aws ec2 run-instances dry-run oracle ($REGION) =="
AMI=$(aws ec2 describe-images --region "$REGION" --owners 099720109477 \
  --filters "Name=name,Values=ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*" "Name=state,Values=available" \
  --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text 2>/dev/null)
say "AMI=$AMI"
if [ -n "$AMI" ] && [ "$AMI" != "None" ]; then
  aws ec2 run-instances --dry-run --region "$REGION" --image-id "$AMI" \
    --instance-type g4dn.xlarge --min-count 1 --max-count 1 --output json 2>&1 | head -8 >> "$RAW"
else
  say "AMI_LOOKUP_UNAVAILABLE"
fi

say "== gcp identity =="
GCP_PROJECT=""
GCP_SA=""
if [ -n "$GOOGLE_APPLICATION_CREDENTIALS" ] && [ -f "$GOOGLE_APPLICATION_CREDENTIALS" ]; then
  GCP_PROJECT=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('project_id') or '')" "$GOOGLE_APPLICATION_CREDENTIALS" 2>/dev/null)
  GCP_SA=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('client_email') or '')" "$GOOGLE_APPLICATION_CREDENTIALS" 2>/dev/null)
fi
say "GCP_PROJECT=$GCP_PROJECT"
say "GCP_SA=$GCP_SA"
gcloud auth list --format="value(account)" >> "$RAW" 2>&1
gcloud config list >> "$RAW" 2>&1

say "== gcp enabled services (first 40) =="
gcloud services list --enabled --project "$GCP_PROJECT" --limit=40 --format="value(config.name)" 2>&1 | head -40 >> "$RAW"

for r in us-central1 us-east4 europe-west4 europe-west1 asia-east1 us-west1; do
  say "== gcp region $r GPU quotas =="
  gcloud compute regions describe "$r" --project "$GCP_PROJECT" --format=json 2>/dev/null | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
except Exception:
    print('REGION_JSON_UNAVAILABLE'); sys.exit(0)
qs=d.get('quotas',[])
found=False
for q in qs:
    m=q.get('metric','')
    if 'GPU' in m or 'gpu' in m:
        found=True
        print('%s limit=%s usage=%s' % (m, q.get('limit'), q.get('usage')))
if not found:
    print('NO_GPU_QUOTA_BUCKETS')
" >> "$RAW" 2>&1
done

say "== gcp aiplatform us-central1 =="
gcloud ai custom-jobs list --region=us-central1 --project "$GCP_PROJECT" --limit=5 --format="value(name,state)" 2>&1 | head -10 >> "$RAW"
gcloud ai endpoints list --region=us-central1 --project "$GCP_PROJECT" --limit=5 --format="value(name)" 2>&1 | head -10 >> "$RAW"

say "== azure account =="
az account show --output json 2>&1 | head -c 1200 >> "$RAW"
say ""
say "== azure subscriptions =="
az account list --all --query '[].{id:id,name:name,state:state,isDefault:isDefault}' --output json 2>&1 | head -c 1500 >> "$RAW"
say ""
for loc in uksouth eastus eastus2 westeurope northeurope; do
  say "== az vm list-usage $loc (NC/ND/NV/GPU) =="
  az vm list-usage --location "$loc" --query "[?contains(localName,'NC')||contains(localName,'ND')||contains(localName,'NV')||contains(localName,'GPU')].{name:localName,current:currentValue,limit:limit}" --output json 2>&1 | head -c 1500 >> "$RAW"
  say ""
done

# Emit a JSON string map (required by the terraform external data source) on stdout.
python3 - "$RAW" <<'PYEOF'
import json, sys
raw = open(sys.argv[1], encoding='utf-8', errors='replace').read()
sections = {}
cur = 'header'
buf = []
for line in raw.splitlines():
    if line.startswith('== ') and line.rstrip().endswith(' =='):
        if buf:
            sections[cur] = '\n'.join(buf)
            buf = []
        cur = line.strip().strip('=').strip().replace(' ', '_').replace('/', '_')
    else:
        buf.append(line)
if buf:
    sections[cur] = '\n'.join(buf)
out = {}
for k, v in sections.items():
    key = k[:60]
    out[key] = v[:1500]
print(json.dumps(out))
PYEOF
exit 0
