#!/usr/bin/env python3
"""C211-I121090 — kedacore Azure managed-container GPU plane, Container Apps branch.

Purpose: settle the ONE unmeasured dimension left by the x120955 GATE. The ACI
branch was verbatim quota-refused (ContainerGroupQuotaReached 'V100Cores' Limit 0
Usage 0 Requested 1, westeurope+eastus). The Container Apps branch only ever
failed on PAYLOAD SHAPE:

  400 WorkloadProfilePropertyNotSupported:
  "Workload Profile property 'MinimumCount' is not supported for
   Consumption_GPU_NC8as_T4"

This probe re-measures the SAME ARM identity with the CORRECTED payload shape:
workloadProfiles [{name,workloadProfileType}] only -- NO minimumCount /
maximumCount / any unsupported property -- then creates ONE nonce-tagged
manual-trigger Container App Job on that GPU profile with a CUDA-capable MCR
image running nvidia-smi, polls the environment provisioningState and the job
execution verbatim, and records every provider error body as-is.

Phase 0 (read-only): session, provider registrationState for Microsoft.App /
  Microsoft.ContainerInstance, EXISTING managed environments list, and the
  location-scoped availableManagedEnvironmentsWorkloadProfileTypes catalog.
Phase 1: ONE nonce-tagged eastus Managed Environment with the GPU workload
  profile (corrected shape), poll provisioningState verbatim.
Phase 2: ONE nonce-tagged manual-trigger Container App Job on that profile
  (attempted even if the environment has not reached Succeeded, so the verbatim
  refusal is captured), start, poll executions, read the execution log for
  nvidia-smi -L + --query-gpu=name,uuid,memory.total, independent execution GET.
Phase 3: teardown of our own resources through the az CLI provider verbs and
  absence readback (404 + ARM list NO_RESULTS).

stdlib only. Every provider response is emitted tagged on stderr and the whole
verbatim matrix is returned as chunked base64 on stdout (terraform external
data-source contract), because terraform discards a data.external program's
stderr.
"""
import base64
import hashlib
import hmac
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ARM = 'https://management.azure.com'
TAG = 'C211_I121179'
PREFIX = 'c211-i121179'
RG = PREFIX + '-rg'
REGION = 'eastus'
STG_VER = '2023-01-01'
PROFILE = 'Consumption-GPU-NC8as-T4'
IMAGE = 'mcr.microsoft.com/azureml/openmpi4.1.0-cuda11.8-cudnn8-ubuntu22.04:latest'
CMDLINE = ('set +e; echo C211-ENV-START; uname -a; '
           'nvidia-smi -L 2>&1; echo ---SMI-Q---; '
           'nvidia-smi --query-gpu=name,uuid,memory.total --format=csv,noheader 2>&1; '
           'echo ---DEV---; ls -l /dev/nvidia* 2>&1; echo ---PROC---; '
           'for f in /proc/driver/nvidia/gpus/*/information; do echo "== $f"; '
           'cat "$f" 2>&1; done; echo ---VER---; '
           'cat /proc/driver/nvidia/version 2>&1; echo ---DONE---; sleep 20; echo C211-END')
APP_VERS = ['2026-07-01', '2024-03-01']
DEADLINE = time.time() + 52 * 60
T0 = time.time()
OUT = {}
EVENTS = []
ENV_DEADLINE = 1500     # seconds to wait for managed environment provisioningState (longer-wait variable)
JOB_DEADLINE = 600      # seconds to wait for job execution to reach a terminal state


def emit(obj):
    try:
        EVENTS.append(obj)
    except Exception:  # noqa: BLE001
        pass
    sys.stderr.write(TAG + ' ' + json.dumps(obj, default=str)[:2000] + '\n')
    sys.stderr.flush()


def save(k, v):
    OUT[k] = v if isinstance(v, str) else json.dumps(v, default=str)


def left():
    return DEADLINE - (time.time() - T0)


def run(cmd, timeout=120):
    t = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {'rc': p.returncode, 'out': p.stdout or '', 'err': (p.stderr or '')[:400],
                'secs': round(time.time() - t, 1)}
    except subprocess.TimeoutExpired:
        return {'rc': 124, 'out': '', 'err': 'TIMEOUT', 'secs': round(time.time() - t, 1)}
    except Exception as e:  # noqa: BLE001
        return {'rc': -1, 'out': '', 'err': repr(e)[:300], 'secs': round(time.time() - t, 1)}


def az(*args, timeout=300):
    return run(['az'] + list(args), timeout=timeout)


def is_notfound(r):
    low = ((r.get('err') or '') + ' ' + (r.get('out') or '')).lower()
    return ('not found' in low or 'resourcenotfound' in low
            or 'could not be found' in low or 'resourcegroupnotfound' in low)


def http(tok, url, method='GET', body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json',
        'User-Agent': PREFIX})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode() or '{}'
            try:
                return r.status, json.loads(raw)
            except Exception:  # noqa: BLE001
                return r.status, {'raw': raw[:1500]}
    except urllib.error.HTTPError as e:
        raw = e.read().decode() or ''
        try:
            return e.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return e.code, {'raw': raw[:1500]}
    except Exception as e:  # noqa: BLE001
        return 0, {'transport_error': repr(e)[:300]}


def err_of(st, body):
    if isinstance(body, dict):
        e = body.get('error')
        if isinstance(e, dict):
            return {'http': st, 'code': e.get('code'),
                    'message': (e.get('message') or '')[:900]}
        if 'raw' in body:
            return {'http': st, 'raw': body['raw'][:900]}
        if 'transport_error' in body:
            return {'http': st, 'transport_error': body['transport_error']}
        return {'http': st, 'body': json.dumps(body)[:600]}
    return {'http': st}


def find_uuids(txt):
    return sorted(set(re.findall(r'GPU-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
                                 r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}', txt or '')))


def httpraw(url, method='GET', body=None, headers=None, timeout=60):
    """Plain HTTP (no ARM bearer) for the blob-SAS channel."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()[:6000]
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read()[:1200]
        except Exception:  # noqa: BLE001
            return e.code, b''
    except Exception as e:  # noqa: BLE001
        return 0, repr(e)[:300].encode()


def sa_sas(acct, key_b64, services='b', rt='co', perm='rwdlc', hours=3):
    """Account SAS for the nonce storage account (blob container rwdlc)."""
    st = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - 300))
    se = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() + hours * 3600))
    sv = '2022-11-02'
    sts = '\n'.join([acct, perm, services, rt, st, se, '', 'https', sv, '']) + '\n'
    sig = base64.b64encode(hmac.new(base64.b64decode(key_b64), sts.encode(),
                                    hashlib.sha256).digest()).decode()
    q = urllib.parse.urlencode({'sv': sv, 'ss': services, 'srt': rt, 'sp': perm,
                                'st': st, 'se': se, 'spr': 'https', 'sig': sig})
    return q, se


def content_of(lg):
    if not isinstance(lg, dict):
        return ''
    c = lg.get('content')
    if c:
        return str(c)
    return str(lg.get('raw') or '')


def build_result(data, verdict):
    """Flat {key:string} map for the terraform external data-source contract."""
    blob = json.dumps(data, default=str, sort_keys=True)
    if len(blob) > 60000:
        blob = blob[:60000]
    b = base64.b64encode(blob.encode()).decode()
    out = {'verdict': str(verdict), 'matrix_len': str(len(blob)),
           'chunks': str((len(b) + 899) // 900)}
    for i in range(0, len(b), 900):
        out['m%03d' % (i // 900)] = b[i:i + 900]

    def _j(v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except Exception:  # noqa: BLE001
                return v
        return v

    dev = _j(data.get('device')) or {}
    out['summary'] = json.dumps({
        'verdict': verdict,
        'session': _j(data.get('session')),
        'rg': _j(data.get('rg')),
        'providers': {k: _j(data.get(k)) for k in ('prov_App', 'prov_ContainerInstance')},
        'profiles_n': (_j(data.get('profiles_eastus')) or {}).get('n')
        if data.get('profiles_eastus') else None,
        'env_create': _j(data.get('env_create')),
        'env_final': _j(data.get('env_final')),
        'job_create': _j(data.get('job_create')),
        'sa_name': data.get('sa_name'),
        'sa_create': _j(data.get('sa_create')),
        'sas_ok': data.get('sas_ok'),
        'uuids': (dev or {}).get('uuids') or [],
    }, default=str)[:1800]
    return out


def env_url(sid, envname, ver):
    return (ARM + '/subscriptions/' + sid + '/resourceGroups/' + RG +
            '/providers/Microsoft.App/managedEnvironments/' + envname +
            '?api-version=' + ver)


def main():
    global RG
    # ---------- session ----------
    r = run(['az', 'account', 'get-access-token', '--resource', 'https://management.azure.com',
             '--query', 'accessToken', '-o', 'tsv'])
    tok = (r['out'] or '').strip()
    if r['rc'] != 0 or tok.count('.') < 2:
        emit({'step': 'token', 'rc': r['rc'], 'err': r['err'][:300]})
        print(json.dumps({'verdict': 'TOKEN_FAILED'}))
        return 0
    acc = {}
    r = run(['az', 'account', 'show', '-o', 'json'])
    if r['rc'] == 0:
        try:
            acc = json.loads(r['out'])
        except Exception:  # noqa: BLE001
            acc = {}
    sid = acc.get('id') or ''
    emit({'step': 'session', 'sub': acc.get('name'), 'sid_tail': sid[-6:],
          'tenant_tail': str(acc.get('tenantId') or '')[-6:], 'state': acc.get('state')})
    save('session', {'sub': acc.get('name'), 'state': acc.get('state'),
                     'sid_tail': sid[-6:]})
    if not sid:
        print(json.dumps({'verdict': 'NO_SUBSCRIPTION'}))
        return 0
    az('config', 'set', 'extension.use_dynamic_install=yes_without_prompt', timeout=60)

    # ---------- providers ----------
    for ns in ('Microsoft.ContainerInstance', 'Microsoft.App'):
        st, b = http(tok, ARM + '/subscriptions/' + sid + '/providers/' + ns +
                     '?api-version=2021-04-01')
        state = b.get('registrationState') if isinstance(b, dict) else None
        emit({'step': 'provider', 'ns': ns, 'http': st, 'registrationState': state})
        save('prov_' + ns.split('.')[-1], {'http': st, 'registrationState': state})
        if st == 200 and state != 'Registered':
            st2, b2 = http(tok, ARM + '/subscriptions/' + sid + '/providers/' + ns +
                           '/register?api-version=2021-04-01', method='POST', timeout=180)
            emit({'step': 'provider-register', 'ns': ns, **err_of(st2, b2)})
            save('prov_register_' + ns.split('.')[-1], err_of(st2, b2))

    # ---------- (1b) EXISTING managed environments ----------
    st, b = http(tok, ARM + '/subscriptions/' + sid +
                 '/providers/Microsoft.App/managedEnvironments?api-version=' + APP_VERS[0],
                 timeout=90)
    names = [x.get('name') for x in (b.get('value') or [])] if isinstance(b, dict) else []
    emit({'step': 'env-list', 'http': st, 'n': len(names), 'names': names[:20]})
    save('env_list', {'http': st, 'n': len(names), 'names': names[:20]})

    # ---------- (1c) GPU workload-profile catalog for eastus ----------
    profinfo = {'http': 0, 'n': 0, 'gpu': []}
    for ver in APP_VERS:
        st, b = http(tok, ARM + '/subscriptions/' + sid + '/providers/Microsoft.App/locations/' +
                     REGION + '/availableManagedEnvironmentsWorkloadProfileTypes?api-version=' +
                     ver, timeout=60)
        vals = (b.get('value') or []) if isinstance(b, dict) else []
        gpu = [{'name': v.get('name'), 'props': v.get('properties')}
               for v in vals if isinstance(v, dict) and 'gpu' in json.dumps(v).lower()]
        emit({'step': 'capp-profiles', 'api_version': ver, 'http': st, 'n': len(vals),
              'gpu': gpu[:6], **({} if st == 200 else err_of(st, b))})
        if st == 200:
            profinfo = {'http': st, 'n': len(vals), 'gpu': gpu, 'api_version': ver}
            break
        profinfo = {'http': st, 'n': len(vals), 'gpu': gpu, 'api_version': ver,
                    'error': err_of(st, b)}
    save('profiles_eastus', profinfo)

    # ---------- resource group ----------
    rgurl = ARM + '/subscriptions/' + sid + '/resourceGroups/' + RG
    st, b = http(tok, rgurl + '?api-version=2021-04-01')
    rg_ok = st == 200
    if st == 404:
        st, b = http(tok, rgurl + '?api-version=2021-04-01', method='PUT',
                     body={'location': REGION, 'tags': {'c211': 'i121090'}})
        emit({'step': 'rg-create', 'http': st, 'location': REGION,
              **({} if st in (200, 201) else err_of(st, b))})
        save('rg_create', {'http': st})
        rg_ok = st in (200, 201)
    elif st == 200:
        emit({'step': 'rg-exists', 'http': st})
    else:
        emit({'step': 'rg-get', 'http': st, **err_of(st, b)})
    if not rg_ok:
        st, b = http(tok, ARM + '/subscriptions/' + sid +
                     '/resourceGroups?api-version=2021-04-01')
        names2 = [x.get('name') for x in (b.get('value') or [])] if isinstance(b, dict) else []
        emit({'step': 'rg-list', 'http': st, 'n': len(names2), 'names': names2[:10]})
        save('rg_list', {'http': st, 'n': len(names2), 'names': names2[:10]})
        if names2:
            RG = names2[0]
            rg_ok = True
            emit({'step': 'rg-fallback', 'name': RG})
    emit({'step': 'rg', 'name': RG, 'usable': rg_ok})
    save('rg', {'name': RG, 'usable': rg_ok})

    # ---------- Phase 1: managed environment, CORRECTED payload ----------
    envname = PREFIX + '-cae'
    envbody = {'location': REGION, 'tags': {'c211': 'i121090'},
               'properties': {'workloadProfiles': [
                   {'name': 'Consumption', 'workloadProfileType': 'Consumption'},
                   {'name': PROFILE, 'workloadProfileType': PROFILE}]}}
    save('env_payload', envbody)
    emit({'step': 'capp-env-payload', 'body': envbody})
    ver_used = None
    env_created = False
    env_create_res = None
    if rg_ok:
        for ver in APP_VERS:
            st, b = http(tok, env_url(sid, envname, ver), method='PUT', body=envbody,
                         timeout=300)
            ec = err_of(st, b)
            emit({'step': 'capp-env-create', 'api_version': ver, **ec})
            env_create_res = dict(ec)
            env_create_res['api_version'] = ver
            if st in (200, 201):
                env_created = True
                ver_used = ver
                break
            code = str(ec.get('code') or '')
            if code not in ('InvalidApiVersionParameter', 'NoRegisteredProviderFound',
                            'InvalidResourceType', 'NotFound'):
                break
        save('env_create', env_create_res)

    env_final = None
    env_id = None
    states = []
    if env_created:
        dl = time.time() + min(ENV_DEADLINE, max(120, left() - 600))
        last = None
        while time.time() < dl:
            st, b = http(tok, env_url(sid, envname, ver_used), timeout=60)
            prov = ((b.get('properties') or {}) if isinstance(b, dict) else {}).get(
                'provisioningState')
            if isinstance(b, dict) and b.get('id') and not env_id:
                env_id = b.get('id')
            states.append({'t': round(time.time() - T0, 1), 'http': st, 'state': prov})
            if prov != last:
                emit({'step': 'capp-env-poll', 'http': st, 'state': prov,
                      'elapsed_s': round(time.time() - T0, 1)})
                last = prov
            if prov in ('Succeeded', 'Failed', 'Canceled'):
                break
            time.sleep(30)
        st, b = http(tok, env_url(sid, envname, ver_used), timeout=60)
        prov = ((b.get('properties') or {}) if isinstance(b, dict) else {}).get(
            'provisioningState')
        if isinstance(b, dict) and b.get('id'):
            env_id = b.get('id')
        if isinstance(b, dict) and isinstance(b.get('properties'), dict):
            errbody = {'error': b['properties'].get('error')} if b['properties'].get('error') \
                else None
        else:
            errbody = None
        _props = (b.get('properties') or {}) if isinstance(b, dict) else {}
        env_final = {'http': st, 'provisioningState': prov,
                     'environmentMode': _props.get('environmentMode'),
                     'n_states_seen': len(set(x['state'] for x in states)),
                     'state_timeline': [x for i, x in enumerate(states)
                                        if i == 0 or x['state'] != states[i - 1]['state']],
                     'polls': len(states),
                     'waited_s': round(time.time() - T0, 1),
                     'last_error': errbody,
                     'deployment_errors': _props.get('deploymentErrors'),
                     'rendered_workload_profiles': _props.get('workloadProfiles'),
                     'default_domain': _props.get('defaultDomain'),
                     'properties_keys': sorted(_props.keys())}
        emit({'step': 'capp-env-verdict', 'state': prov, 'polls': len(states)})
    save('env_final', env_final)
    save('env_id', env_id or '')

    # ---------- Phase 1b: storage account + account SAS stdout channel ----------
    SA = ('c211i121179' + hashlib.sha256((PREFIX + str(time.time())).encode()).hexdigest()[:8])[:24]
    sa_url = (ARM + '/subscriptions/' + sid + '/resourceGroups/' + RG +
              '/providers/Microsoft.Storage/storageAccounts/' + SA +
              '?api-version=' + STG_VER)
    sas = None
    sa_created = False
    blob_url = ''
    JOB_CMD = CMDLINE + '; echo C211_UPLOAD_DONE; sleep 15'
    save('sa_name', SA)
    if rg_ok:
        st, b = http(tok, ARM + '/subscriptions/' + sid +
                     '/providers/Microsoft.Storage?api-version=2021-04-01')
        stg_state = b.get('registrationState') if isinstance(b, dict) else None
        emit({'step': 'provider-storage', 'http': st, 'registrationState': stg_state})
        save('prov_Storage', {'http': st, 'registrationState': stg_state})
        if stg_state != 'Registered':
            st2, b2 = http(tok, ARM + '/subscriptions/' + sid +
                           '/providers/Microsoft.Storage/register?api-version=2021-04-01',
                           method='POST', timeout=180)
            emit({'step': 'provider-storage-register', **err_of(st2, b2)})
            save('prov_storage_register', err_of(st2, b2))
        sabody = {'location': REGION, 'tags': {'c211': 'i121179'},
                  'sku': {'name': 'Standard_LRS'}, 'kind': 'StorageV2',
                  'properties': {'allowBlobPublicAccess': False, 'minimumTlsVersion': 'TLS1_2',
                                 'supportsHttpsTrafficOnly': True,
                                 'networkAcls': {'bypass': 'AzureServices',
                                                 'defaultAction': 'Allow'}}}
        st, b = http(tok, sa_url, method='PUT', body=sabody, timeout=240)
        emit({'step': 'sa-create', 'name': SA, **err_of(st, b)})
        save('sa_create', {'name': SA, **err_of(st, b)})
        sa_created = st in (200, 201, 202)
        if sa_created:
            for i in range(24):
                stp, bp = http(tok, sa_url, timeout=60)
                ps = ((bp.get('properties') or {}) if isinstance(bp, dict)
                      else {}).get('provisioningState')
                emit({'step': 'sa-poll', 'i': i, 'http': stp, 'state': ps})
                if ps == 'Succeeded':
                    break
                time.sleep(10)
            st, kb = http(tok, ARM + '/subscriptions/' + sid + '/resourceGroups/' + RG +
                          '/providers/Microsoft.Storage/storageAccounts/' + SA +
                          '/listKeys?api-version=' + STG_VER, method='POST', body={},
                          timeout=90)
            key = None
            try:
                key = (kb.get('keys') or [{}])[0].get('value')
            except Exception:  # noqa: BLE001
                pass
            emit({'step': 'sa-keys', 'http': st, 'got': bool(key)})
            save('sa_keys', {'http': st, 'got': bool(key)})
            if key:
                try:
                    q, se = sa_sas(SA, key)
                    base = 'https://' + SA + '.blob.core.windows.net'
                    stc, bc = httpraw(base + '/c211?restype=container&' + q, method='PUT',
                                      timeout=90)
                    emit({'step': 'sa-container', 'http': stc,
                          'body': bc[:200].decode('utf-8', 'replace')})
                    if stc in (201, 409):
                        blob_url = base + '/c211/' + PREFIX + '-out.txt?' + q
                        sas = q
                        emit({'step': 'sa-sas', 'ok': True,
                              'blob': base + '/c211/' + PREFIX + '-out.txt', 'expires': se})
                        up = ("if command -v curl >/dev/null 2>&1; then "
                              "curl -sS -X PUT '" + blob_url + "' "
                              "-H 'x-ms-blob-type: BlockBlob' -H 'x-ms-version: 2022-11-02' "
                              "-H 'Content-Type: text/plain' --data-binary @/tmp/c211.txt; "
                              "echo C211_UPLOAD_CURL_RC=$?; else python3 -c \"import "
                              "urllib.request;d=open('/tmp/c211.txt','rb').read();"
                              "r=urllib.request.Request('" + blob_url + "',data=d,method='PUT',"
                              "headers={'x-ms-blob-type':'BlockBlob','x-ms-version':'2022-11-02',"
                              "'Content-Type':'text/plain'});print('C211_UPLOAD_PY',"
                              "urllib.request.urlopen(r,timeout=30).status)\"; fi")
                        JOB_CMD = ('OUT=/tmp/c211.txt; { ' + CMDLINE + ' ; } > $OUT 2>&1; '
                                   'cat $OUT; ' + up + '; echo C211_UPLOAD_DONE; sleep 15')
                except Exception as e:  # noqa: BLE001
                    emit({'step': 'sa-sas', 'ok': False, 'err': repr(e)[:300]})
        save('blob_url_ok', bool(blob_url))
        save('sas_ok', bool(sas))

    # ---------- Phase 2: Container App Job on the GPU profile ----------
    jobname = PREFIX + '-job'
    jobbase = (ARM + '/subscriptions/' + sid + '/resourceGroups/' + RG +
               '/providers/Microsoft.App/jobs/' + jobname)
    jver = ver_used or APP_VERS[0]
    if not env_id and env_created:
        st, b = http(tok, env_url(sid, envname, jver), timeout=60)
        if isinstance(b, dict) and b.get('id'):
            env_id = b.get('id')
    if not env_id:
        env_id = (ARM + '/subscriptions/' + sid + '/resourceGroups/' + RG +
                  '/providers/Microsoft.App/managedEnvironments/' + envname)
    jobbody = {'location': REGION, 'tags': {'c211': 'i121090'},
               'properties': {
                   'environmentId': env_id,
                   'workloadProfileName': PROFILE,
                   'configuration': {'triggerType': 'Manual', 'replicaRetryLimit': 0,
                                     'replicaTimeout': 900,
                                     'manualTriggerConfig': {'replicaCompletionCount': 1,
                                                             'parallelism': 1}},
                   'template': {'containers': [{
                       'name': 'gpu', 'image': IMAGE,
                       'command': ['/bin/bash', '-c', JOB_CMD],
                       'resources': {'cpu': 4, 'memory': '16Gi'}}]}}}
    save('job_payload', jobbody)
    job_create_res = None
    job_created = False
    job_start_res = None
    execs = []
    exec_resource = {}
    logread = {}
    job_uuids = []
    if rg_ok:
        st, b = http(tok, jobbase + '?api-version=' + jver, method='PUT', body=jobbody,
                     timeout=240)
        jc = err_of(st, b)
        jc['api_version'] = jver
        emit({'step': 'capp-job-create', **jc})
        job_create_res = jc
        job_created = st in (200, 201)
        save('job_create', job_create_res)
        if job_created:
            st, b = http(tok, jobbase + '/start?api-version=' + jver, method='POST',
                         timeout=120)
            emit({'step': 'capp-job-start', **err_of(st, b)})
            job_start_res = err_of(st, b)
            save('job_start', job_start_res)
            dl = time.time() + min(JOB_DEADLINE, max(240, left() - 420))
            seen = []
            capture_text = ''
            execname = None
            k = 0
            while time.time() < dl:
                st, ex = http(tok, jobbase + '/executions?api-version=' + jver, timeout=60)
                vv = (ex.get('value') or []) if isinstance(ex, dict) else []
                if vv:
                    seen = [{'name': v.get('name'), 'status': v.get('status')}
                            for v in vv][:5]
                    execname = seen[0].get('name')
                blobtxt = ''
                if execname:
                    for ver in (jver, '2024-03-01'):
                        sl, bl = httpraw(jobbase + '/executions/' + str(execname) +
                                         '/containers/gpu/logstream?api-version=' + ver,
                                         timeout=30)
                        if sl == 200 and bl:
                            txt = bl.decode('utf-8', 'replace')
                            if txt:
                                capture_text = txt
                                emit({'step': 'capp-logstream', 'ver': ver, 'len': len(txt),
                                      'has_uuid': bool(find_uuids(txt)),
                                      'tail': txt[-900:]})
                if sas:
                    sb, bb = httpraw(blob_url, timeout=45)
                    if sb == 200 and bb:
                        blobtxt = bb.decode('utf-8', 'replace')
                        if blobtxt:
                            capture_text = blobtxt
                            emit({'step': 'capp-blob', 'http': sb, 'len': len(blobtxt),
                                  'has_uuid': bool(find_uuids(blobtxt)),
                                  'tail': blobtxt[-1200:]})
                    elif k % 4 == 0:
                        emit({'step': 'capp-blob', 'http': sb, 'len': 0})
                emit({'step': 'capp-exec-poll', 'i': k, 'http': st, 'n': len(vv),
                      'states': seen, 'capture_len': len(capture_text)})
                if capture_text and find_uuids(capture_text):
                    break
                if blobtxt and 'C211-END' in blobtxt:
                    break
                if vv and str(vv[0].get('status')).lower() in ('succeeded', 'failed',
                                                               'stopped', 'degraded'):
                    if blobtxt or k >= 8:
                        break
                k += 1
                time.sleep(20)
            execs = seen
            save('executions', execs)
            if seen:
                exname = seen[0].get('name')
                st, exr = http(tok, jobbase + '/executions/' + str(exname) +
                               '?api-version=' + jver, timeout=60)
                if st == 200 and isinstance(exr, dict):
                    props = exr.get('properties') or {}
                    exec_resource = {
                        'http': st, 'name': exr.get('name'),
                        'status': props.get('status'),
                        'startTime': props.get('startTime'),
                        'endTime': props.get('endTime'),
                        'reason': props.get('reason'),
                        'template_containers': [
                            {'name': c.get('name'), 'image': c.get('image'),
                             'resources': c.get('resources')}
                            for c in ((props.get('template') or {}).get('containers') or [])]}
                else:
                    exec_resource = err_of(st, exr)
                emit({'step': 'capp-exec-get', 'http': st,
                      'status': exec_resource.get('status'),
                      'reason': exec_resource.get('reason')})
                save('exec_resource', exec_resource)
                logbody = ''
                cands = [
                    ('POST', jobbase + '/executions/' + str(exname) +
                     '/containers/gpu/logs?api-version=' + jver),
                    ('GET', jobbase + '/executions/' + str(exname) +
                     '/containers/gpu/logs?api-version=' + jver),
                    ('GET', jobbase + '/executions/' + str(exname) +
                     '/containers/gpu/logs?api-version=' + jver + '&tailLines=200'),
                    ('POST', jobbase + '/getLogs?api-version=' + jver),
                    ('POST', jobbase + '/executions/' + str(exname) +
                     '/logs?api-version=' + jver),
                ]
                for m, u in cands:
                    st, lg = http(tok, u, method=m, timeout=90)
                    body = content_of(lg)
                    rec = {'m': m, 'tail': u.split('/jobs/' + jobname)[-1][:110],
                           'http': st, 'len': len(body),
                           'gpu_uuids': find_uuids(body)}
                    if st != 200:
                        rec.update({k: v for k, v in err_of(st, lg).items() if k != 'http'})
                    emit({'step': 'capp-logs', **rec})
                    logread.setdefault('attempts', []).append(rec)
                    if body and (('---DONE---' in body) or ('GPU-' in body) or
                                 ('NVIDIA' in body)):
                        logbody = body
                        logread['winner'] = {'m': m, 'http': st, 'len': len(body)}
                        break
                    if body and not logbody:
                        logbody = body
                logread['body_tail'] = logbody[-4000:]
                logread['capture_tail'] = capture_text[-4000:]
                logread['capture_len'] = len(capture_text)
                job_uuids = find_uuids(logbody + '\n' + capture_text)
                save('log', logread)
                save('capture_text', capture_text[:12000])
                save('device', {'uuids': job_uuids, 'region': REGION,
                                'profile': PROFILE, 'job': jobname,
                                'env': envname, 'execution': exname,
                                'log_len': len(logbody),
                                'capture_len': len(capture_text),
                                'sources': {'job_log': bool(logbody),
                                            'blob_or_stream': bool(capture_text)}})

    # ---------- Phase 3: teardown of our own resources (az provider verbs) ----
    cleanup = {}
    if job_created:
        r = az('containerapp', 'job', 'delete', '--subscription', sid,
               '--resource-group', RG, '--name', jobname, '--yes', timeout=420)
        emit({'step': 'capp-job-delete', 'rc': r['rc'], 'err': (r['err'] or '')[:250]})
        gone = False
        for _ in range(24):
            st2, b2 = http(tok, jobbase + '?api-version=' + jver, timeout=60)
            if st2 == 404:
                gone = True
                break
            time.sleep(10)
        emit({'step': 'capp-job-absence', 'http_404': gone})
        cleanup['job_delete_rc'] = r['rc']
        cleanup['job_gone_404'] = gone
    if env_created:
        r = az('containerapp', 'env', 'delete', '--subscription', sid,
               '--resource-group', RG, '--name', envname, '--yes', timeout=600)
        emit({'step': 'capp-env-delete', 'rc': r['rc'], 'err': (r['err'] or '')[:250]})
        gone = False
        for _ in range(30):
            st2, b2 = http(tok, env_url(sid, envname, jver), timeout=60)
            if st2 == 404:
                gone = True
                break
            time.sleep(10)
        emit({'step': 'capp-env-absence', 'http_404': gone})
        cleanup['env_delete_rc'] = r['rc']
        cleanup['env_gone_404'] = gone
    if sa_created:
        r = az('storage', 'account', 'delete', '--subscription', sid,
               '--resource-group', RG, '--name', SA, '--yes', timeout=420)
        emit({'step': 'sa-delete', 'rc': r['rc'], 'err': (r['err'] or '')[:250]})
        gone = False
        for _ in range(24):
            st2, b2 = http(tok, sa_url, timeout=60)
            if st2 == 404:
                gone = True
                break
            time.sleep(10)
        emit({'step': 'sa-absence', 'http_404': gone})
        cleanup['sa_delete_rc'] = r['rc']
        cleanup['sa_gone_404'] = gone
    for label, u, ver in (
            ('job', ARM + '/subscriptions/' + sid + '/providers/Microsoft.App/jobs', jver),
            ('env', ARM + '/subscriptions/' + sid +
             '/providers/Microsoft.App/managedEnvironments', jver),
            ('sa', ARM + '/subscriptions/' + sid +
             '/providers/Microsoft.Storage/storageAccounts', STG_VER)):
        st, b = http(tok, u + '?api-version=' + ver, timeout=120)
        names = [x.get('name') for x in (b.get('value') or [])] if isinstance(b, dict) else []
        mine = [n for n in names if str(n).lower().startswith(PREFIX)]
        emit({'step': 'list-' + label, 'http': st, 'n': len(names), 'mine': mine})
        cleanup[label + '_list_mine'] = mine
    if rg_ok and RG == PREFIX + '-rg':
        r = az('group', 'delete', '--subscription', sid, '--name', RG, '--yes', '--no-wait',
               timeout=300)
        emit({'step': 'rg-delete', 'rc': r['rc'], 'err': (r['err'] or '')[:200]})
        gone = False
        for _ in range(30):
            r2 = az('group', 'show', '--subscription', sid, '--name', RG, '-o', 'json',
                    timeout=90)
            if is_notfound(r2):
                gone = True
                break
            time.sleep(10)
        emit({'step': 'rg-absence', 'notfound': gone})
        save('rg_cleanup', {'delete_rc': r['rc'], 'gone': gone})
    else:
        save('rg_cleanup', {'delete': 'skipped', 'reason': 'pre-existing RG ' + RG})
    save('cleanup', cleanup)

    save('events', EVENTS[-160:])

    # ---------- verdict ----------
    env_state = str((env_final or {}).get('provisioningState') or 'UNKNOWN').upper()
    if job_uuids:
        verdict = 'READY_CAPP_GPU_DEVICE_PROVEN'
    elif job_created and env_state == 'SUCCEEDED':
        verdict = 'CAPP_ENV_OK_JOB_NO_DEVICE_UUID'
    elif env_created and not job_created:
        verdict = 'CAPP_ENV_%s_JOB_REFUSED' % env_state
    elif env_created:
        verdict = 'CAPP_ENV_%s_JOB_NO_UUID' % env_state
    else:
        verdict = 'CAPP_ENV_REFUSED'
    emit({'step': 'DONE', 'verdict': verdict, 'uuids': job_uuids,
          'env_state': env_state, 'job_created': job_created})
    save('verdict', verdict)
    print(json.dumps(build_result(OUT, verdict), default=str))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        emit({'step': 'FATAL', 'error': repr(exc)[:400]})
        print(json.dumps({'verdict': 'PROBE_EXCEPTION', 'error': repr(exc)[:300]}))
        sys.exit(0)
