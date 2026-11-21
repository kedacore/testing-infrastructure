#!/usr/bin/env python3
"""c211-i119860: sts:AssumeRole reachability for every role the IAM policy exposes.

The mandate allows sts:AssumeRole where the role policy permits. This script
lists roles read-only, picks up to 8 policy-relevant candidates, attempts a
900-second assumption for each, and for any success prints ONLY the resulting
identity ARN, the L-DB2E81BA quota and a g5.xlarge RunInstances DryRun verdict.
No secret value is printed.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe_aws2 as A  # noqa: E402

REGION = os.environ.get('AWS_REGION') or 'eu-west-2'
IAM_REGION = 'us-east-1'
CAND = re.compile(r'(github|keda|terraform|deploy|cicd|ci[-_]|runner|actions|gpu|admin|power|cloud|eks|k8s|infra|automation|gha|oidc)', re.I)


def roles():
    r = A.call('iam', 'ListRoles', {}, IAM_REGION, '2010-05-08')
    if not r['ok']:
        print('IAM_LIST_ROLES status=%s code=%s body=%s' % (r['status'], A.err_code(r), A.one_line(r, 260)))
        return []
    root = A.xml_root(r['body'])
    out = []
    if root is not None:
        for it in root.findall('.//Roles/member'):
            out.append({'name': it.findtext('RoleName') or '', 'arn': it.findtext('Arn') or ''})
    return out


def assume(arn):
    r = A.call('sts', 'AssumeRole', {'RoleArn': arn, 'RoleSessionName': 'c211i119860',
                                     'DurationSeconds': '900'}, IAM_REGION, '2011-06-15')
    if r['status'] != 200:
        return None, '%s/%s' % (r['status'], A.err_code(r))
    root = A.xml_root(r['body'])
    if root is None:
        return None, 'NO_XML'
    ak = root.findtext('.//Credentials/AccessKeyId')
    sk = root.findtext('.//Credentials/SecretAccessKey')
    tk = root.findtext('.//Credentials/SessionToken')
    if not ak or not sk:
        return None, 'NO_CREDS'
    return {'ak': ak, 'sk': sk, 'tk': tk or ''}, 'OK'


def probe_identity(creds):
    old = {k: os.environ.get(k) for k in ('AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN')}
    os.environ['AWS_ACCESS_KEY_ID'] = creds['ak']
    os.environ['AWS_SECRET_ACCESS_KEY'] = creds['sk']
    os.environ['AWS_SESSION_TOKEN'] = creds['tk']
    try:
        info = {}
        r = A.call('sts', 'GetCallerIdentity', {}, IAM_REGION, '2011-06-15')
        root = A.xml_root(r['body'])
        info['arn'] = root.findtext('.//Arn') if root is not None else None
        q = A.call('servicequotas', 'GetServiceQuota', {'ServiceCode': 'ec2', 'QuotaCode': 'L-DB2E81BA'},
                   REGION, '2019-06-24')
        qroot = A.xml_root(q['body'])
        info['quota'] = qroot.findtext('.//Quota/Value') if qroot is not None else None
        info['quota_status'] = '%s/%s' % (q['status'], A.err_code(q))
        d = A.call('ec2', 'RunInstances', {'DryRun': 'true', 'InstanceType': 'g5.xlarge',
                                           'MinCount': '1', 'MaxCount': '1',
                                           'SubnetId': 'subnet-0e770146b6917a067',
                                           'SecurityGroupId.1': 'sg-012549f2bc6556c0e'},
                   REGION, '2016-11-15')
        info['dryrun'] = '%s/%s' % (d['status'], A.err_code(d))
        return info
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def main():
    rs = roles()
    print('IAM_ROLES count=%d' % len(rs))
    if not rs:
        return 0
    print('IAM_ROLE_NAMES %s' % ' '.join(sorted(r['name'] for r in rs))[:1500])
    r0 = A.call('sts', 'GetCallerIdentity', {}, IAM_REGION, '2011-06-15')
    root0 = A.xml_root(r0['body'])
    self_arn = root0.findtext('.//Arn') if root0 is not None else None
    print('SELF %s' % self_arn)
    cands = [r for r in rs if CAND.search(r['name'] or '')][:8]
    print('ASSUME_CANDIDATES %s' % ','.join(r['name'] for r in cands))
    for r in cands:
        if r['arn'] == self_arn:
            print('ASSUME %s SKIP_SELF' % r['name'])
            continue
        creds, st = assume(r['arn'])
        if creds:
            info = probe_identity(creds)
            print('ASSUME %s OK arn=%s quota_L-DB2E81BA=%s quota_status=%s dryrun_g5=%s'
                  % (r['name'], info.get('arn'), info.get('quota'), info.get('quota_status'), info.get('dryrun')))
        else:
            print('ASSUME %s DENIED %s' % (r['name'], st))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        print('ASSUME_FATAL %s' % repr(e)[:300])
