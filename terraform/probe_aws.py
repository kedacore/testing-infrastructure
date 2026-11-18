#!/usr/bin/env python3
"""c211-i109237 - minimal read-only AWS query-API client (SigV4, stdlib only).

Independent of the `aws` CLI so that a CLI shim / wrapper cannot mask the real
IAM verdict. Prints API verdict lines to stdout; never prints credential values.
Actions:
  sts                                  -> GetCallerIdentity (control)
  offerings <region> <t1,t2,...>       -> DescribeInstanceTypeOfferings
  vpcs <region> | subnets <region> | sgs <region>
  dryrun <region> <type> [ami]         -> RunInstances DryRun=true (no resource created)
"""
import datetime
import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

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
    body_params = dict(params)
    body_params['Action'] = action
    body_params['Version'] = version
    body = '&'.join('%s=%s' % (urllib.parse.quote(k, safe='-_.~'),
                               urllib.parse.quote(str(v), safe='-_.~'))
                    for k, v in sorted(body_params.items()))
    payload_hash = hashlib.sha256(body.encode('utf-8')).hexdigest()
    headers = {'content-type': 'application/x-www-form-urlencoded; charset=utf-8',
               'host': host,
               'x-amz-date': amzdate}
    if tok:
        headers['x-amz-security-token'] = tok
    names = sorted(headers)
    canonical_headers = ''.join('%s:%s\n' % (n, headers[n].strip()) for n in names)
    signed_headers = ';'.join(names)
    canonical_request = '\n'.join(['POST', '/', '', canonical_headers, signed_headers, payload_hash])
    scope = '%s/%s/%s/aws4_request' % (datestamp, region, service)
    string_to_sign = '\n'.join(['AWS4-HMAC-SHA256', amzdate, scope,
                                hashlib.sha256(canonical_request.encode('utf-8')).hexdigest()])
    sig = hmac.new(_signing_key(sk, datestamp, region, service),
                   string_to_sign.encode('utf-8'), hashlib.sha256).hexdigest()
    auth = ('AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s'
            % (ak, scope, signed_headers, sig))
    send_headers = {'Content-Type': headers['content-type'],
                    'Host': host,
                    'X-Amz-Date': amzdate,
                    'Authorization': auth}
    if tok:
        send_headers['X-Amz-Security-Token'] = tok
    req = urllib.request.Request('https://%s/' % host, data=body.encode('utf-8'),
                                 headers=send_headers, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode('utf-8', 'replace')
            return {'ok': True, 'status': resp.status, 'body': raw[:1200], 'error': None}
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', 'replace')
        return {'ok': False, 'status': e.code, 'body': raw[:1200], 'error': 'HTTPError'}
    except Exception as e:  # network/timeout
        return {'ok': False, 'status': None, 'body': '', 'error': repr(e)[:200]}


def verdict(res):
    """Extract the AWS error Code (or success marker) from a query-API response."""
    body = res.get('body') or ''
    code = None
    if '<Code>' in body:
        code = body.split('<Code>', 1)[1].split('</Code>', 1)[0]
    elif res.get('status') == 200:
        code = 'OK_200'
    return code


def main(argv):
    if len(argv) < 2:
        print('usage: probe_aws.py <action> ...')
        return 2
    act = argv[1]
    region = argv[2] if len(argv) > 2 and argv[2] else REGION_DEFAULT

    if act == 'sts':
        r = call('sts', 'GetCallerIdentity', {}, region, '2011-06-15')
        print('SIGV4_STS status=%s code=%s error=%s body=%s' % (r['status'], verdict(r), r['error'], r['body'].replace('\n', ' ')[:500]))
        return 0

    if act == 'offerings':
        types = argv[3] if len(argv) > 3 else 'g6e.2xlarge,p4d.24xlarge,p5.48xlarge,g6e.12xlarge'
        params = {'LocationType': 'region'}
        for i, t in enumerate(types.split(',')):
            params['Filter.%d.Name' % (i + 1)] = 'instance-type'
            params['Filter.%d.Value.%d' % (i + 1, 1)] = t
        r = call('ec2', 'DescribeInstanceTypeOfferings', params, region, '2016-11-15')
        offered = []
        if r['body']:
            for chunk in r['body'].split('<item>'):
                if '<instanceType>' in chunk:
                    offered.append(chunk.split('<instanceType>', 1)[1].split('</instanceType>', 1)[0])
        print('SIGV4_OFFERINGS status=%s code=%s offered=%s'
              % (r['status'], verdict(r), ','.join(offered) if offered else 'NONE_PARSED'))
        if not offered:
            print('SIGV4_OFFERINGS_RAW %s' % r['body'].replace('\n', ' ')[:600])
        return 0

    if act in ('vpcs', 'subnets', 'sgs'):
        action = {'vpcs': 'DescribeVpcs', 'subnets': 'DescribeSubnets',
                  'sgs': 'DescribeSecurityGroups'}[act]
        r = call('ec2', action, {}, region, '2016-11-15')
        body = r['body'] or ''
        out = {'status': r['status'], 'code': verdict(r), 'count': body.count('<item>')}
        if act == 'vpcs':
            out['default_vpc'] = 'true' if '<isDefault>true</isDefault>' in body else 'false'
            ids = []
            for chunk in body.split('<item>'):
                if '<vpcId>' in chunk:
                    ids.append(chunk.split('<vpcId>', 1)[1].split('</vpcId>', 1)[0])
            out['ids'] = ids[:10]
        if act == 'subnets':
            subs = []
            for chunk in body.split('<item>'):
                if '<subnetId>' in chunk:
                    subs.append({'id': chunk.split('<subnetId>', 1)[1].split('</subnetId>', 1)[0],
                                 'vpc': (chunk.split('<vpcId>', 1)[1].split('</vpcId>', 1)[0]
                                         if '<vpcId>' in chunk else None),
                                 'az': (chunk.split('<availabilityZone>', 1)[1].split('</availabilityZone>', 1)[0]
                                        if '<availabilityZone>' in chunk else None),
                                 'defaultForAz': ('true' if '<defaultForAz>true</defaultForAz>' in chunk else 'false')})
            out['subnets'] = subs[:15]
        if act == 'sgs':
            gs = []
            for chunk in body.split('<item>'):
                if '<groupId>' in chunk:
                    gs.append({'id': chunk.split('<groupId>', 1)[1].split('</groupId>', 1)[0],
                               'name': (chunk.split('<groupName>', 1)[1].split('</groupName>', 1)[0]
                                        if '<groupName>' in chunk else None),
                               'vpc': (chunk.split('<vpcId>', 1)[1].split('</vpcId>', 1)[0]
                                       if '<vpcId>' in chunk else None)})
            out['sgs'] = gs[:15]
        print('SIGV4_%s %s' % (act.upper(), json.dumps(out)[:1100]))
        return 0

    if act == 'dryrun':
        itype = argv[3] if len(argv) > 3 else 'g6e.2xlarge'
        ami = argv[4] if len(argv) > 4 else ''
        params = {'DryRun': 'true', 'InstanceType': itype, 'MinCount': '1', 'MaxCount': '1'}
        if ami and ami.startswith('ami-'):
            params['ImageId'] = ami
        r = call('ec2', 'RunInstances', params, region, '2016-11-15')
        code = verdict(r)
        meaning = ('AUTHORIZED' if code == 'DryRunOperation' else
                   'DENIED' if code in ('UnauthorizedOperation', 'AccessDenied', 'Blocked') else
                   'PARAM_OR_OTHER:%s' % code)
        print('SIGV4_DRYRUN type=%s ami=%s status=%s code=%s meaning=%s error=%s body=%s'
              % (itype, ami, r['status'], code, meaning, r['error'], r['body'].replace('\n', ' ')[:700]))
        return 0

    print('unknown action %s' % act)
    return 2


if __name__ == '__main__':
    sys.exit(main(sys.argv))
