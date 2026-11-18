#!/usr/bin/env python3
"""c211-i109237 - minimal AWS query-API client (SigV4, stdlib only), phase 2.

Adds explicit SubnetId/SecurityGroupId DryRun, a strictly gated one-shot marker
launch, console-output readback and termination. Never prints credential values.
"""
import base64
import datetime
import hashlib
import hmac
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

REGION_DEFAULT = os.environ.get('AWS_REGION') or os.environ.get('AWS_DEFAULT_REGION') or 'eu-west-2'


def _sign(key, msg):
    return hmac.new(key, msg.encode('utf-8'), hashlib.sha256).digest()


def _signing_key(secret, datestamp, region, service):
    k = _sign(('AWS4' + secret).encode('utf-8'), datestamp)
    k = _sign(k, region)
    k = _sign(k, service)
    return _sign(k, 'aws4_request')


def call(service, action, params, region, version, host=None):
    ak = os.environ.get('AWS_ACCESS_KEY_ID') or ''
    sk = os.environ.get('AWS_SECRET_ACCESS_KEY') or ''
    tok = os.environ.get('AWS_SESSION_TOKEN') or ''
    if not ak or not sk:
        return {'ok': False, 'error': 'NO_CREDS_IN_ENV', 'status': None, 'body': ''}
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
    if tok:
        headers['x-amz-security-token'] = tok
    names = sorted(headers)
    ch = ''.join('%s:%s\n' % (n, headers[n].strip()) for n in names)
    sh = ';'.join(names)
    cr = '\n'.join(['POST', '/', '', ch, sh, ph])
    scope = '%s/%s/%s/aws4_request' % (datestamp, region, service)
    sts = '\n'.join(['AWS4-HMAC-SHA256', amzdate, scope,
                     hashlib.sha256(cr.encode('utf-8')).hexdigest()])
    sig = hmac.new(_signing_key(sk, datestamp, region, service), sts.encode('utf-8'),
                   hashlib.sha256).hexdigest()
    send = {'Content-Type': headers['content-type'], 'Host': host,
            'X-Amz-Date': amzdate,
            'Authorization': 'AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s'
                             % (ak, scope, sh, sig)}
    if tok:
        send['X-Amz-Security-Token'] = tok
    req = urllib.request.Request('https://%s/' % host, data=body.encode('utf-8'),
                                 headers=send, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            return {'ok': True, 'status': resp.status,
                    'body': resp.read().decode('utf-8', 'replace'), 'error': None}
    except urllib.error.HTTPError as e:
        return {'ok': False, 'status': e.code,
                'body': e.read().decode('utf-8', 'replace'), 'error': 'HTTPError'}
    except Exception as e:
        return {'ok': False, 'status': None, 'body': '', 'error': repr(e)[:200]}


def xml_root(body):
    try:
        b = re.sub(r'\sxmlns="[^"]*"', '', body or '', count=3)
        return ET.fromstring(b)
    except Exception:
        return None


def err_code(res):
    root = xml_root(res.get('body'))
    if root is not None:
        e = root.find('.//Error/Code')
        if e is not None and e.text:
            return e.text
    if res.get('status') == 200:
        return 'OK_200'
    return None


def one_line(res, n=700):
    return (res.get('body') or '').replace('\n', ' ').replace('\r', ' ')[:n]


def find_all(root, tag):
    return [e for e in root.iter() if e.tag.endswith(tag)]


def collect_inventory(region):
    inv = {'vpcs': [], 'subnets': [], 'sgs': []}
    r = call('ec2', 'DescribeVpcs', {}, region, '2016-11-15')
    root = xml_root(r['body'])
    if root is not None:
        for it in root.findall('.//vpcSet/item'):
            inv['vpcs'].append({'id': (it.findtext('vpcId') or '').strip(),
                                'default': (it.findtext('isDefault') or '').strip(),
                                'cidr': (it.findtext('cidrBlock') or '').strip()})
    r = call('ec2', 'DescribeSubnets', {}, region, '2016-11-15')
    root = xml_root(r['body'])
    if root is not None:
        for it in root.findall('.//subnetSet/item'):
            inv['subnets'].append({'id': (it.findtext('subnetId') or '').strip(),
                                   'vpc': (it.findtext('vpcId') or '').strip(),
                                   'az': (it.findtext('availabilityZone') or '').strip(),
                                   'dflt': (it.findtext('defaultForAz') or '').strip(),
                                   'state': (it.findtext('state') or '').strip(),
                                   'free_ips': (it.findtext('availableIpAddressCount') or '').strip()})
    r = call('ec2', 'DescribeSecurityGroups', {}, region, '2016-11-15')
    root = xml_root(r['body'])
    if root is not None:
        for it in root.findall('.//securityGroupInfo/item'):
            inv['sgs'].append({'id': (it.findtext('groupId') or '').strip(),
                               'name': (it.findtext('groupName') or '').strip(),
                               'vpc': (it.findtext('vpcId') or '').strip()})
    return inv


def pick_target(inv):
    """Prefer a DefaultForAz subnet, then any; pair it with a same-VPC 'default' SG."""
    subs = [s for s in inv['subnets'] if s['state'] in ('available', '') and s['id'].startswith('subnet-')]
    subs.sort(key=lambda s: (0 if s['dflt'] == 'true' else 1, s['id']))
    for s in subs:
        cand = [g for g in inv['sgs'] if g['vpc'] == s['vpc']]
        cand.sort(key=lambda g: (0 if g['name'] == 'default' else 1, g['id']))
        if cand:
            return {'subnet': s, 'sg': cand[0]}
    if subs:
        return {'subnet': subs[0], 'sg': None}
    return None


def main(argv):
    act = argv[1] if len(argv) > 1 else ''
    region = argv[2] if len(argv) > 2 and argv[2] else REGION_DEFAULT

    if act == 'sts':
        r = call('sts', 'GetCallerIdentity', {}, region, '2011-06-15')
        print('SIGV4_STS status=%s code=%s error=%s body=%s' % (r['status'], err_code(r), r['error'], one_line(r, 400)))
        return 0

    if act == 'inventory':
        inv = collect_inventory(region)
        print('SIGV4_INVENTORY %s' % json.dumps(inv)[:1800])
        t = pick_target(inv)
        print('SIGV4_TARGET %s' % json.dumps(t)[:500] if t else 'SIGV4_TARGET none')
        return 0

    if act == 'dryrun2':
        # dryrun2 <region> <type> <ami> <subnet> <sg>
        itype = argv[3]
        ami = argv[4]
        subnet = argv[5] if len(argv) > 5 else ''
        sg = argv[6] if len(argv) > 6 else ''
        params = {'DryRun': 'true', 'InstanceType': itype, 'MinCount': '1', 'MaxCount': '1'}
        if ami.startswith('ami-'):
            params['ImageId'] = ami
        if subnet.startswith('subnet-'):
            params['SubnetId'] = subnet
        if sg.startswith('sg-'):
            params['SecurityGroupId.1'] = sg
        r = call('ec2', 'RunInstances', params, region, '2016-11-15')
        code = err_code(r)
        meaning = ('AUTHORIZED' if code == 'DryRunOperation' else
                   'DENIED' if code in ('UnauthorizedOperation', 'AccessDenied', 'Blocked') else
                   'PARAM_OR_OTHER:%s' % code)
        print('SIGV4_DRYRUN2 type=%s subnet=%s sg=%s status=%s code=%s meaning=%s body=%s'
              % (itype, subnet, sg, r['status'], code, meaning, one_line(r, 500)))
        return 0

    if act == 'run':
        # run <region> <type> <ami> <subnet> <sg> <userdata_file> <tag>
        itype, ami, subnet, sg, udfile, tag = argv[3], argv[4], argv[5], argv[6], argv[7], argv[8]
        ud = base64.b64encode(open(udfile, 'rb').read()).decode() if os.path.exists(udfile) else ''
        params = {'ImageId': ami, 'InstanceType': itype, 'MinCount': '1', 'MaxCount': '1',
                  'InstanceInitiatedShutdownBehavior': 'terminate',
                  'TagSpecification.1.ResourceType': 'instance',
                  'TagSpecification.1.Tag.1.Key': 'Name',
                  'TagSpecification.1.Tag.1.Value': tag,
                  'TagSpecification.2.ResourceType': 'volume',
                  'TagSpecification.2.Tag.1.Key': 'Name',
                  'TagSpecification.2.Tag.1.Value': tag}
        if ud:
            params['UserData'] = ud
        if subnet.startswith('subnet-'):
            params['SubnetId'] = subnet
        if sg.startswith('sg-'):
            params['SecurityGroupId.1'] = sg
        r = call('ec2', 'RunInstances', params, region, '2016-11-15')
        iid = None
        root = xml_root(r['body'])
        if root is not None:
            iid = root.findtext('.//instancesSet/item/instanceId')
        print('SIGV4_RUN status=%s code=%s instance=%s body=%s'
              % (r['status'], err_code(r), iid, one_line(r, 500)))
        if iid:
            print('C211_MARKER_INSTANCE_ID=%s' % iid)
        return 0

    if act == 'describe':
        r = call('ec2', 'DescribeInstances', {'InstanceId.1': argv[3]}, region, '2016-11-15')
        root = xml_root(r['body'])
        state, ipub, itype, launch = None, None, None, None
        if root is not None:
            it = root.find('.//instancesSet/item')
            if it is not None:
                state = (it.findtext('instanceState/name') or '').strip()
                ipub = (it.findtext('ipAddress') or '').strip()
                itype = (it.findtext('instanceType') or '').strip()
                launch = (it.findtext('launchTime') or '').strip()
        print('SIGV4_DESCRIBE id=%s status=%s code=%s state=%s type=%s publicIp=%s launch=%s'
              % (argv[3], r['status'], err_code(r), state, itype, ipub, launch))
        return 0

    if act == 'console':
        r = call('ec2', 'GetConsoleOutput', {'InstanceId': argv[3]}, region, '2016-11-15')
        root = xml_root(r['body'])
        out = ''
        if root is not None:
            b64 = (root.findtext('.//output') or '').strip()
            if b64:
                try:
                    out = base64.b64decode(b64).decode('utf-8', 'replace')
                except Exception:
                    out = 'DECODE_FAILED'
        keep = [ln for ln in out.splitlines()
                if ('C211_MARKER' in ln or 'GPU-' in ln or 'NVIDIA' in ln
                    or 'nvidia-smi' in ln or 'MIG' in ln)]
        print('SIGV4_CONSOLE id=%s status=%s code=%s decoded_bytes=%d kept_lines=%d'
              % (argv[3], r['status'], err_code(r), len(out), len(keep)))
        for ln in keep[:25]:
            print('CONSOLE| %s' % ln.strip()[:220])
        if not out:
            print('CONSOLE_EMPTY body=%s' % one_line(r, 300))
        return 0

    if act == 'terminate':
        r = call('ec2', 'TerminateInstances', {'InstanceId.1': argv[3]}, region, '2016-11-15')
        root = xml_root(r['body'])
        st = None
        if root is not None:
            it = root.find('.//instancesSet/item')
            if it is not None:
                st = (it.findtext('currentState/name') or '').strip()
        print('SIGV4_TERMINATE id=%s status=%s code=%s state=%s body=%s'
              % (argv[3], r['status'], err_code(r), st, one_line(r, 300)))
        return 0

    print('unknown action %r' % act)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv))
