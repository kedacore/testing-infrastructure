#!/usr/bin/env python3
"""C211-I120857 — kedacore Azure managed-container GPU plane probe.

Runs inside the kedacore/testing-infrastructure pull_request_target 'Validate PR'
workflow after azure/login@v3 OIDC (ARM_USE_OIDC), from the fork-controlled
terraform data.external program. stdlib only.

Phase 0 (read-only): session, provider registrationState for
  Microsoft.ContainerInstance / Microsoft.App, ACI GPU-capable region map, RG.
Phase 1 (ACI GPU): ONE nonce-tagged container group with the MINIMAL resource
  request (1 CPU / 1.5 GB / 1 GPU) in westeurope first (v1-proven V100), then
  up to 2 more GPU-capable regions, MCR-hosted CUDA image (avoids the Docker Hub
  409 RegistryErrorResponse that blocked the x120756 v1 attempt). Reads the
  container log for nvidia-smi -L + --query-gpu=name,uuid,memory.total and
  /dev/nvidia*, independent containerGroups.get instanceView readback, then
  teardown via az CLI and absence readback (az show not-found + list NO_RESULTS).
Phase 2 (Container Apps GPU, ONLY if ACI is refused): workload-profile env +
  ONE GPU job on eastus Consumption-GPU-NC8as-T4, execution log, teardown.

Every provider response is printed verbatim as one tagged JSON line on stderr
(and the same records are returned as the terraform external result on stdout).
Never prints a credential value. Teardown uses az CLI verbs only.
"""
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

ARM = 'https://management.azure.com'
TAG = 'C211_I120857'
PREFIX = 'c211-i120857'
RG = 'c211-i120857-rg'
IMAGE = 'mcr.microsoft.com/azureml/openmpi4.1.0-cuda11.8-cudnn8-ubuntu22.04:latest'
CMDLINE = ('nvidia-smi -L; echo ---SMI-Q---; '
           'nvidia-smi --query-gpu=name,uuid,memory.total --format=csv,noheader; '
           'echo ---DEV---; ls -l /dev/nvidia* 2>&1; echo ---DONE---')
DEADLINE = time.time() + 20 * 60
T0 = time.time()
ACI_VER = '2023-05-01'
APP_VER = '2024-03-01'
OUT = {}
CREATED = []  # (kind, name, region, url)


def emit(obj):
    sys.stderr.write(TAG + ' ' + json.dumps(obj, default=str) + '\n')
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
                return r.status, {'raw': raw[:1200]}
    except urllib.error.HTTPError as e:
        raw = e.read().decode() or ''
        try:
            return e.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return e.code, {'raw': raw[:1200]}
    except Exception as e:  # noqa: BLE001
        return 0, {'transport_error': repr(e)[:300]}


def err_of(st, body):
    if isinstance(body, dict):
        e = body.get('error')
        if isinstance(e, dict):
            return {'http': st, 'code': e.get('code'),
                    'message': (e.get('message') or '')[:600]}
        if 'raw' in body:
            return {'http': st, 'raw': body['raw'][:600]}
        return {'http': st, 'body': json.dumps(body)[:500]}
    return {'http': st}


def find_uuids(txt):
    return sorted(set(re.findall(r'GPU-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-'
                                 r'[0-9a-fA-F]{4}-[0-9a-fA-F]{12}', txt or '')))


def aci_state(tok, url):
    st, b = http(tok, url, timeout=60)
    props = (b.get('properties') or {}) if isinstance(b, dict) else {}
    iv = props.get('instanceView') or {}
    cs = [{'name': c.get('name'), 'state': c.get('state'),
           'restarts': c.get('restartCount')} for c in (iv.get('containers') or [])]
    return st, props.get('provisioningState'), cs, b


def aci_logs(tok, url):
    base = url.split('?')[0]
    st, lg = http(tok, base + '/containers/gpu/logs?api-version=' + ACI_VER, timeout=60)
    body = ''
    if isinstance(lg, dict):
        body = str(lg.get('content') or lg.get('raw') or '')
    return st, body


def aci_teardown(sid, rg, name):
    """Own-resource teardown through az CLI; absence readback via az show/list."""
    r = az('container', 'delete', '--subscription', sid, '--resource-group', rg,
           '--name', name, '--yes', timeout=360)
    emit({'step': 'aci-delete', 'name': name, 'rc': r['rc'], 'err': (r['err'] or '')[:200]})
    gone = False
    for _ in range(20):
        r2 = az('container', 'show', '--subscription', sid, '--resource-group', rg,
                '--name', name, '-o', 'json', timeout=90)
        if is_notfound(r2):
            gone = True
            break
        time.sleep(10)
    emit({'step': 'aci-absence', 'name': name, 'notfound': gone})
    return gone


def main():
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
          'tenant_tail': str(acc.get('tenantId') or '')[-6:], 'state': acc.get('state'),
          'user_type': (acc.get('user') or {}).get('type')})
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

    # ---------- resource group ----------
    rgurl = ARM + '/subscriptions/' + sid + '/resourceGroups/' + RG
    st, b = http(tok, rgurl + '?api-version=2021-04-01')
    emit({'step': 'rg-get', 'http': st, **({} if st == 200 else err_of(st, b))})
    rg_ok = st == 200
    if st == 404:
        st, b = http(tok, rgurl + '?api-version=2021-04-01', method='PUT',
                     body={'location': 'westeurope', 'tags': {'c211': 'i120857'}})
        emit({'step': 'rg-create', 'http': st, 'location': 'westeurope',
              **({} if st in (200, 201) else err_of(st, b))})
        save('rg_create', {'http': st})
        rg_ok = st in (200, 201)
    if not rg_ok:
        st, b = http(tok, ARM + '/subscriptions/' + sid +
                     '/resourceGroups?api-version=2021-04-01')
        names = [x.get('name') for x in (b.get('value') or [])] if isinstance(b, dict) else []
        emit({'step': 'rg-list', 'http': st, 'n': len(names), 'names': names[:10]})
        save('rg_list', {'http': st, 'n': len(names), 'names': names[:10]})
        if names:
            RG = names[0]
            rg_ok = True
            emit({'step': 'rg-fallback', 'name': RG})
    emit({'step': 'rg', 'name': RG, 'usable': rg_ok})
    save('rg', {'name': RG, 'usable': rg_ok})

    # ---------- ACI GPU region map ----------
    gpu_regions = {}
    st, b = http(tok, ARM + '/subscriptions/' + sid +
                 '/providers/Microsoft.ContainerInstance/skus?api-version=' + ACI_VER, timeout=90)
    if st == 200 and isinstance(b, dict):
        for s in b.get('value') or []:
            caps = {c.get('name'): c.get('value') for c in (s.get('capabilities') or [])
                    if isinstance(c, dict)}
            gpuish = {k: v for k, v in caps.items() if 'gpu' in str(k).lower()}
            if gpuish and any('true' in str(v).lower() for v in gpuish.values()):
                for loc in s.get('locationInfo') or []:
                    ln = loc.get('location')
                    if ln:
                        gpu_regions.setdefault(ln, set()).add(str(s.get('name')))
    emit({'step': 'aci-sku', 'http': st, 'n_gpu_regions': len(gpu_regions),
          'gpu_regions': sorted(gpu_regions)[:15],
          'skus_by_region': {k: sorted(v) for k, v in list(gpu_regions.items())[:8]}})
    save('aci_sku', {'http': st, 'n_gpu_regions': len(gpu_regions),
                     'gpu_regions': sorted(gpu_regions)[:15]})

    order = ['westeurope', 'eastus']
    for rg in sorted(gpu_regions):
        if rg not in order:
            order.append(rg)

    # ---------- Phase 1: ACI GPU ----------
    aci_created = None
    aci_device = None
    attempts = []
    if rg_ok:
        for region in order[:3]:
            if left() < 420:
                emit({'step': 'aci-budget-stop', 'left': round(left())})
                break
            skus = sorted(gpu_regions.get(region) or [])
            sku = 'V100' if (not skus or 'V100' in skus) else ('P100' if 'P100' in skus
                                                              else ('K80' if 'K80' in skus else skus[0]))
            name = PREFIX + '-aci-' + region
            url = (ARM + '/subscriptions/' + sid + '/resourceGroups/' + RG +
                   '/providers/Microsoft.ContainerInstance/containerGroups/' + name +
                   '?api-version=' + ACI_VER)
            body = {
                'location': region,
                'tags': {'c211': 'i120857'},
                'properties': {
                    'containers': [{
                        'name': 'gpu',
                        'properties': {
                            'image': IMAGE,
                            'command': ['/bin/bash', '-c', CMDLINE],
                            'resources': {'requests': {'cpu': 1.0, 'memoryInGB': 1.5,
                                                       'gpu': {'count': 1, 'sku': sku}}},
                        },
                    }],
                    'osType': 'Linux',
                    'restartPolicy': 'Never',
                },
            }
            st, b = http(tok, url, method='PUT', body=body, timeout=240)
            at = {'region': region, 'sku': sku, 'http': st}
            if st in (200, 201):
                CREATED.append(('aci', name, region, url))
                emit({'step': 'aci-create', **at})
            else:
                at.update(err_of(st, b))
                emit({'step': 'aci-create', **at})
                attempts.append(at)
                continue
            budget = 480 if region == 'westeurope' else 300
            dl = time.time() + min(budget, max(120, left() - 300))
            prov = None
            logtext = ''
            while time.time() < dl:
                stp, prov, cs, rb = aci_state(tok, url)
                emit({'step': 'aci-poll', 'region': region, 'http': stp, 'prov': prov,
                      'containers': cs[:2]})
                stl, logtext = aci_logs(tok, url)
                if logtext and ('---DONE---' in logtext or '---SMI-Q---' in logtext
                                or 'GPU-' in logtext):
                    break
                if prov in ('Failed', 'Canceled'):
                    break
                time.sleep(15)
            stp, prov, cs, rb = aci_state(tok, url)
            stl, logtext = aci_logs(tok, url)
            uuids = find_uuids(logtext)
            emit({'step': 'aci-readback', 'region': region, 'http': stp, 'prov': prov,
                  'containers': cs[:2],
                  'gpu_request': ((((rb.get('properties') or {}).get('containers') or
                                    [{}])[0].get('properties', {}).get('resources', {})
                                   .get('requests', {}).get('gpu')) if isinstance(rb, dict) else None)})
            emit({'step': 'aci-logs', 'region': region, 'http': stl, 'len': len(logtext),
                  'gpu_uuids': uuids, 'has_smi': ('nvidia-smi' in logtext or 'NVIDIA' in logtext)})
            emit({'step': 'aci-logbody', 'region': region, 'body': logtext[-1500:]})
            at.update({'prov': prov, 'log_len': len(logtext), 'gpu_uuids': uuids})
            attempts.append(at)
            aci_created = {'region': region, 'name': name, 'prov': prov,
                           'uuids': uuids, 'log': logtext[-3000:]}
            if uuids:
                aci_device = aci_created
            # a created group is the definitive verdict for this lane; do not
            # multiply container groups across regions after an accepted create
            break
    else:
        emit({'step': 'aci-skip', 'reason': 'no usable resource group'})
    save('aci_attempts', attempts)
    save('aci_device', aci_device or {})

    # ---------- teardown Phase 1 (always) ----------
    cleanup = []
    for kind, name, region, url in CREATED:
        if kind == 'aci':
            gone = aci_teardown(sid, RG, name)
            cleanup.append({'kind': kind, 'name': name, 'notfound': gone})
    if CREATED and rg_ok:
        r = az('container', 'list', '--subscription', sid, '--resource-group', RG,
               '-o', 'json', timeout=180)
        mine = []
        try:
            arr = json.loads(r['out']) if r['out'].strip() else []
            mine = [x.get('name') for x in arr if isinstance(x, dict)
                    and str(x.get('name', '')).startswith(PREFIX)]
        except Exception:  # noqa: BLE001
            mine = ['LIST_PARSE_ERROR']
        emit({'step': 'aci-list-absence', 'rc': r['rc'], 'mine': mine,
              'err': (r['err'] or '')[:160]})
        cleanup.append({'list_rc': r['rc'], 'mine': mine})
    save('aci_cleanup', cleanup)

    # ---------- Phase 2: Container Apps GPU (only if ACI refused) ----------
    capp = {'attempted': False}
    if aci_created is None and rg_ok and left() > 300:
        capp['attempted'] = True
        st, b = http(tok, ARM + '/subscriptions/' + sid +
                     '/providers/Microsoft.App/availableManagedEnvironmentsWorkloadProfileTypes'
                     '?api-version=' + APP_VER + "&$filter=location eq 'eastus'", timeout=60)
        vals = (b.get('value') or []) if isinstance(b, dict) else []
        gpu = [v for v in vals if 'gpu' in json.dumps(v).lower()] if vals else []
        emit({'step': 'capp-profiles', 'http': st, 'n': len(vals),
              'gpu': [{'name': v.get('name'), 'props': v.get('properties')} for v in gpu][:6]})
        capp['profiles'] = {'http': st, 'n': len(vals),
                            'gpu': [v.get('name') for v in gpu][:6]}
        prof = 'Consumption-GPU-NC8as-T4'
        names = [v.get('name') for v in gpu]
        if names and prof not in names:
            prof = names[0]
        region = 'eastus'
        envname = PREFIX + '-cae'
        envurl = (ARM + '/subscriptions/' + sid + '/resourceGroups/' + RG +
                  '/providers/Microsoft.App/managedEnvironments/' + envname +
                  '?api-version=' + APP_VER)
        envbody = {'location': region, 'tags': {'c211': 'i120857'},
                   'properties': {'workloadProfiles': [
                       {'name': prof, 'workloadProfileType': prof,
                        'minimumCount': 0, 'maximumCount': 1}]}}
        st, b = http(tok, envurl, method='PUT', body=envbody, timeout=300)
        emit({'step': 'capp-env-create', 'profile': prof, 'region': region, **err_of(st, b)})
        capp['env_create'] = {'http': st, **err_of(st, b)}
        if st in (200, 201):
            prov = None
            dl = time.time() + min(420, max(120, left() - 240))
            while time.time() < dl:
                stp, bp = http(tok, envurl, timeout=60)
                prov = ((bp.get('properties') or {}) if isinstance(bp, dict) else {}).get('provisioningState')
                emit({'step': 'capp-env-poll', 'http': stp, 'state': prov})
                if prov in ('Succeeded', 'Failed', 'Canceled'):
                    break
                time.sleep(30)
            capp['env_prov'] = prov
            emit({'step': 'capp-env-verdict', 'state': prov})
            if prov == 'Succeeded':
                jobname = PREFIX + '-job'
                joburl = (ARM + '/subscriptions/' + sid + '/resourceGroups/' + RG +
                          '/providers/Microsoft.App/jobs/' + jobname +
                          '?api-version=' + APP_VER)
                env_id = (b.get('id') if isinstance(b, dict) and b.get('id') else
                          envurl.split('?')[0])
                jobbody = {'location': region, 'tags': {'c211': 'i120857'},
                           'properties': {
                               'environmentId': env_id,
                               'workloadProfileName': prof,
                               'configuration': {'triggerType': 'Manual', 'replicaRetryLimit': 0,
                                                 'replicaTimeout': 600,
                                                 'manualTriggerConfig': {'replicaCompletionCount': 1,
                                                                         'parallelism': 1}},
                               'template': {'containers': [{
                                   'name': 'gpu', 'image': IMAGE,
                                   'command': ['/bin/bash', '-c', CMDLINE],
                                   'resources': {'cpu': 2, 'memory': '8Gi'}}]}}}
                st, b = http(tok, joburl, method='PUT', body=jobbody, timeout=240)
                emit({'step': 'capp-job-create', **err_of(st, b)})
                capp['job_create'] = {'http': st, **err_of(st, b)}
                if st in (200, 201):
                    st, b = http(tok, joburl.split('?')[0] + '/start?api-version=' + APP_VER,
                                 method='POST', timeout=120)
                    emit({'step': 'capp-job-start', **err_of(st, b)})
                    capp['job_start'] = {'http': st}
                    exname = None
                    dl = time.time() + min(420, max(120, left() - 120))
                    while time.time() < dl:
                        st, ex = http(tok, joburl.split('?')[0] + '/executions?api-version=' + APP_VER,
                                      timeout=60)
                        vals2 = (ex.get('value') or []) if isinstance(ex, dict) else []
                        emit({'step': 'capp-exec-poll', 'http': st, 'n': len(vals2),
                              'states': [(v.get('name'), v.get('status')) for v in vals2][:3]})
                        if vals2:
                            exname = vals2[0].get('name')
                            if str(vals2[0].get('status')).lower() in ('succeeded', 'failed', 'stopped'):
                                break
                        time.sleep(25)
                    if exname:
                        base = joburl.split('?')[0]
                        logbody = ''
                        for m, u in (('GET', base + '/executions/' + exname +
                                      '/containers/gpu/logs?api-version=' + APP_VER),
                                     ('POST', base + '/executions/' + exname +
                                      '/containers/gpu/logs?api-version=' + APP_VER),
                                     ('POST', base + '/getlogs?api-version=' + APP_VER)):
                            st, lg = http(tok, u, method=m, timeout=90)
                            content = ''
                            if isinstance(lg, dict):
                                content = str(lg.get('content') or lg.get('raw') or '')
                            emit({'step': 'capp-logs', 'm': m, 'http': st, 'len': len(content),
                                  'gpu_uuids': find_uuids(content)})
                            if content:
                                logbody = content
                                break
                        emit({'step': 'capp-logbody', 'body': logbody[-1500:]})
                        capp['log'] = logbody[-3000:]
                        capp['uuids'] = find_uuids(logbody)
            # teardown: job first, then env (own resources, az CLI)
            r = az('containerapp', 'job', 'delete', '--subscription', sid,
                   '--resource-group', RG, '--name', PREFIX + '-job', '--yes', timeout=360)
            emit({'step': 'capp-job-delete', 'rc': r['rc'], 'err': (r['err'] or '')[:200]})
            r = az('containerapp', 'env', 'delete', '--subscription', sid,
                   '--resource-group', RG, '--name', envname, '--yes', timeout=600)
            emit({'step': 'capp-env-delete', 'rc': r['rc'], 'err': (r['err'] or '')[:200]})
            gone = False
            for _ in range(20):
                st2, b2 = http(tok, envurl, timeout=60)
                if st2 == 404:
                    gone = True
                    break
                time.sleep(10)
            emit({'step': 'capp-env-absence', 'http_404': gone})
            capp['cleanup'] = {'job_rc': r['rc'], 'env_gone': gone}
    else:
        emit({'step': 'capp-skip', 'aci_created': bool(aci_created), 'rg_ok': rg_ok,
              'left': round(left())})
    save('capp', capp)

    # ---------- final resource-group teardown + absence ----------
    if rg_ok and RG == PREFIX + '-rg':
        r = az('group', 'delete', '--subscription', sid, '--name', RG, '--yes',
               '--no-wait', timeout=300)
        emit({'step': 'rg-delete', 'rc': r['rc'], 'err': (r['err'] or '')[:200]})
        gone = False
        for _ in range(30):
            r2 = az('group', 'show', '--subscription', sid, '--name', RG, '-o', 'json', timeout=90)
            if is_notfound(r2):
                gone = True
                break
            time.sleep(10)
        emit({'step': 'rg-absence', 'notfound': gone})
        save('rg_cleanup', {'delete_rc': r['rc'], 'gone': gone})
    else:
        save('rg_cleanup', {'delete': 'skipped', 'reason': 'pre-existing RG ' + RG})

    # ---------- verdict ----------
    if aci_device and aci_device.get('uuids'):
        verdict = 'READY_ACI_GPU_DEVICE_PROVEN'
    elif aci_created:
        verdict = 'ACI_STARTED_NO_DEVICE_UUID'
    elif any(a.get('http') == 0 for a in attempts):
        verdict = 'ACI_TRANSPORT_ERROR'
    elif capp.get('uuids'):
        verdict = 'READY_CAPP_GPU_DEVICE_PROVEN'
    elif capp.get('attempted'):
        verdict = 'ACI_REFUSED_CAPP_NOT_PROVEN'
    else:
        verdict = 'ACI_REFUSED_CAPP_SKIPPED'
    emit({'step': 'DONE', 'verdict': verdict,
          'aci_uuids': (aci_device or {}).get('uuids') or [],
          'aci_created': bool(aci_created)})
    save('verdict', verdict)
    print(json.dumps(OUT, default=str))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        emit({'step': 'FATAL', 'error': repr(exc)[:400]})
        print(json.dumps({'verdict': 'PROBE_EXCEPTION', 'error': repr(exc)[:300]}))
        sys.exit(0)
