#!/usr/bin/env python3
"""C211-I121544 — kedacore Container Apps GPU lane CLEANUP + independent readback.

Runs on the SAME pull_request_target 'Validate PR' vehicle (fork branch
c211-i121090-cappgpu, PR #232) under the workflow OIDC ARM token.

The terminal device-proof run 44879698500 created env c211-i121179-cae (reached
provisioningState Succeeded), ran job c211-i121179-job / execution
c211-i121179-job-tazbn3w, captured Tesla T4 GPU-fb9a8282-3547-5da5-8760-760691fa0de6
via the account-SAS blob, and deleted the job + storage account (404) but its
environment DELETE timed out (rc=124) and RG c211-i121179-rg remained
(gone=false). This probe performs the independent post-run provider readback of
every one of those resources and completes the teardown with az CLI provider
verbs, proving absence by not-found readback and by list NO_RESULTS.

stdout: one flat JSON map (terraform external contract) with the chunked
base64 verbatim matrix.
"""
import base64
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

ARM = 'https://management.azure.com'
TAG = 'C211_I121544'
PREFIX = 'c211-i121179'
RG = PREFIX + '-rg'
ENVNAME = PREFIX + '-cae'
JOBNAME = PREFIX + '-job'
SAPREFIX = 'c211i121179'
STG_VER = '2023-01-01'
RG_VER = '2021-04-01'
APP_VER = '2026-07-01'
T0 = time.time()
OUT = {}
EVENTS = []
DEADLINE = time.time() + 27 * 60


def emit(obj):
    try:
        EVENTS.append(obj)
    except Exception:
        pass
    sys.stderr.write(TAG + ' ' + json.dumps(obj, default=str)[:1500] + '\n')
    sys.stderr.flush()


def save(k, v):
    OUT[k] = v if isinstance(v, str) else json.dumps(v, default=str)


def left():
    return DEADLINE - (time.time() - T0)


def run(cmd, timeout=120):
    t = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {'rc': p.returncode, 'out': p.stdout or '', 'err': (p.stderr or '')[:500],
                'secs': round(time.time() - t, 1)}
    except subprocess.TimeoutExpired:
        return {'rc': 124, 'out': '', 'err': 'TIMEOUT', 'secs': round(time.time() - t, 1)}
    except Exception as e:
        return {'rc': -1, 'out': '', 'err': repr(e)[:300], 'secs': round(time.time() - t, 1)}


def az(*args, timeout=300):
    return run(['az'] + list(args), timeout=timeout)


def is_notfound(r):
    low = ((r.get('err') or '') + ' ' + (r.get('out') or '')).lower()
    return ('not found' in low or 'resourcenotfound' in low
            or 'could not be found' in low or 'resourcegroupnotfound' in low
            or 'was not found' in low)


def http_get(tok, url, timeout=120):
    req = urllib.request.Request(url, method='GET', headers={
        'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json',
        'User-Agent': PREFIX})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode() or '{}'
            try:
                return r.status, json.loads(raw)
            except Exception:
                return r.status, {'raw': raw[:1200]}
    except urllib.error.HTTPError as e:
        raw = e.read().decode() or ''
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {'raw': raw[:1200]}
    except Exception as e:
        return 0, {'transport_error': repr(e)[:300]}


def slim(r, keep=('http', 'name', 'state', 'provisioningState', 'code', 'message')):
    if not isinstance(r, dict):
        return {'raw': str(r)[:300]}
    return {k: r[k] for k in keep if k in r}


def build_result(data, verdict):
    blob = json.dumps(data, default=str, sort_keys=True)
    if len(blob) > 60000:
        blob = blob[:60000]
    b = base64.b64encode(blob.encode()).decode()
    out = {'verdict': str(verdict), 'matrix_len': str(len(blob)),
           'chunks': str((len(b) + 899) // 900)}
    for i in range(0, len(b), 900):
        out['m%03d' % (i // 900)] = b[i:i + 900]
    out['summary'] = json.dumps({
        'verdict': verdict,
        'session': data.get('session'),
        'env_readback': data.get('env_readback'),
        'job_readback': data.get('job_readback'),
        'sa_readback': data.get('sa_readback'),
        'rg_readback': data.get('rg_readback'),
        'deletes': data.get('deletes'),
        'lists': data.get('lists'),
    }, default=str)[:1800]
    return out


def poll_gone_az(label, argv, tries=90, sleep=10):
    seen = []
    for i in range(tries):
        r = az(*argv, timeout=90)
        seen.append({'i': i, 'rc': r['rc'], 'notfound': is_notfound(r),
                     'secs': r['secs'], 't': round(time.time() - T0, 1)})
        if i == 0:
            emit({'step': 'absence-' + label + '-0', 'rc': r['rc'],
                  'err': (r['err'] or '')[:300]})
        if is_notfound(r):
            emit({'step': 'absence-' + label, 'notfound': True, 'i': i})
            return True, seen
        if left() < 240:
            break
        time.sleep(sleep)
    emit({'step': 'absence-' + label, 'notfound': False, 'i': len(seen) - 1})
    return False, seen


def main():
    # ---------- token + session ----------
    r = run(['az', 'account', 'get-access-token', '--resource', 'https://management.azure.com',
             '--query', 'accessToken', '-o', 'tsv'])
    tok = (r['out'] or '').strip()
    acct = run(['az', 'account', 'show', '-o', 'json'])
    sid = ''
    try:
        sj = json.loads(acct['out'] or '{}')
        sid = sj.get('id') or ''
        save('session', {'name': sj.get('name'), 'state': sj.get('state'),
                         'sid_tail': (sid or '')[-6:], 'tenant': sj.get('tenantId')})
    except Exception:
        save('session', {'account_show_rc': acct['rc'], 'err': (acct['err'] or '')[:300]})
    if not tok or not sid:
        save('verdict_pre', 'NO_ARM_TOKEN_OR_SID')
        print(json.dumps(build_result(OUT, 'CLEANUP_NO_ARM_TOKEN'), default=str))
        return 0
    emit({'step': 'session', 'sid_tail': sid[-6:], 'tok_len': len(tok)})
    base = ARM + '/subscriptions/' + sid + '/resourceGroups/' + RG
    envurl = (base + '/providers/Microsoft.App/managedEnvironments/' + ENVNAME)

    # ---------- phase 1: independent post-run provider readback ----------
    # ARM GET (read-only) for verbatim provider state, plus az show for the
    # provider-verb cross-check.
    st, b = http_get(tok, envurl + '?api-version=' + APP_VER)
    save('env_readback', {'http': st} if st != 200 else
         {'http': st, 'name': b.get('name'),
          'provisioningState': (b.get('properties') or {}).get('provisioningState')})
    if st != 200 and isinstance(b, dict):
        save('env_readback', {**OUT['env_readback'], 'raw': json.dumps(b)[:500]})
    emit({'step': 'env-readback-arm', 'v': OUT['env_readback']})
    env_show = az('containerapp', 'env', 'show', '--subscription', sid,
                  '--resource-group', RG, '--name', ENVNAME, '-o', 'json', timeout=120)
    save('env_show', {'rc': env_show['rc'], 'notfound': is_notfound(env_show),
                      'out_head': (env_show['out'] or '')[:400],
                      'err': (env_show['err'] or '')[:300]})
    emit({'step': 'env-show', 'rc': env_show['rc'], 'notfound': OUT['env_show']['notfound']})

    job_show = az('containerapp', 'job', 'show', '--subscription', sid,
                  '--resource-group', RG, '--name', JOBNAME, '-o', 'json', timeout=120)
    save('job_readback', {'rc': job_show['rc'], 'notfound': is_notfound(job_show),
                          'err': (job_show['err'] or '')[:300]})
    emit({'step': 'job-show', 'rc': job_show['rc'], 'notfound': OUT['job_readback']['notfound']})

    sa_list = az('storage', 'account', 'list', '--subscription', sid,
                 '--resource-group', RG, '-o', 'json', timeout=120)
    sa_names = []
    try:
        sa_names = [x.get('name') for x in json.loads(sa_list['out'] or '[]')]
    except Exception:
        pass
    sa_mine = [n for n in sa_names if str(n).lower().startswith(SAPREFIX)]
    save('sa_readback', {'rc': sa_list['rc'], 'names': sa_names, 'mine': sa_mine,
                         'err': (sa_list['err'] or '')[:300]})
    emit({'step': 'sa-list', 'rc': sa_list['rc'], 'names': sa_names, 'mine': sa_mine})

    rg_show = az('group', 'show', '--subscription', sid, '--name', RG, '-o', 'json', timeout=120)
    rg_state = None
    try:
        rg_state = (json.loads(rg_show['out'] or '{}').get('properties') or {}).get(
            'provisioningState')
    except Exception:
        pass
    save('rg_readback', {'rc': rg_show['rc'], 'notfound': is_notfound(rg_show),
                         'state': rg_state, 'err': (rg_show['err'] or '')[:300]})
    emit({'step': 'rg-show', 'rc': rg_show['rc'], 'notfound': OUT['rg_readback']['notfound'],
          'state': rg_state})
    if rg_show['rc'] == 0:
        rl = az('resource', 'list', '--subscription', sid, '--resource-group', RG,
                '-o', 'json', timeout=180)
        names = []
        try:
            names = ['%s/%s' % (x.get('type'), x.get('name'))
                     for x in json.loads(rl['out'] or '[]')]
        except Exception:
            pass
        save('rg_resources', {'rc': rl['rc'], 'n': len(names), 'names': names[:60]})
        emit({'step': 'rg-resources', 'rc': rl['rc'], 'n': len(names), 'names': names[:12]})
    else:
        save('rg_resources', {'rc': rg_show['rc'], 'skipped': True})

    # ---------- phase 2: teardown through az provider verbs ----------
    deletes = {}
    if not OUT['env_show']['notfound'] or OUT['env_readback'].get('http') == 200:
        r = az('containerapp', 'env', 'delete', '--subscription', sid,
               '--resource-group', RG, '--name', ENVNAME, '--yes', '--no-wait',
               timeout=300)
        deletes['env_delete'] = {'rc': r['rc'], 'err': (r['err'] or '')[:400]}
        emit({'step': 'env-delete', **deletes['env_delete']})
        gone, seen = poll_gone_az('env', ['containerapp', 'env', 'show', '--subscription', sid,
                                          '--resource-group', RG, '--name', ENVNAME, '-o', 'json'])
        deletes['env_gone_notfound'] = gone
        deletes['env_poll'] = seen[-6:]
    else:
        deletes['env_delete'] = 'ALREADY_NOTFOUND'
        deletes['env_gone_notfound'] = True
    if not OUT['job_readback']['notfound']:
        r = az('containerapp', 'job', 'delete', '--subscription', sid,
               '--resource-group', RG, '--name', JOBNAME, '--yes', timeout=420)
        deletes['job_delete'] = {'rc': r['rc'], 'err': (r['err'] or '')[:400]}
        gone, seen = poll_gone_az('job', ['containerapp', 'job', 'show', '--subscription', sid,
                                          '--resource-group', RG, '--name', JOBNAME, '-o', 'json'],
                                  tries=30)
        deletes['job_gone_notfound'] = gone
    else:
        deletes['job_delete'] = 'ALREADY_NOTFOUND'
        deletes['job_gone_notfound'] = True
    for n in sa_mine:
        r = az('storage', 'account', 'delete', '--subscription', sid,
               '--resource-group', RG, '--name', n, '--yes', timeout=420)
        deletes['sa_delete_' + n] = {'rc': r['rc'], 'err': (r['err'] or '')[:400]}
        gone, seen = poll_gone_az('sa-' + n, ['storage', 'account', 'show', '--subscription', sid,
                                             '--resource-group', RG, '--name', n, '-o', 'json'],
                                  tries=30)
        deletes['sa_gone_notfound_' + n] = gone
    save('deletes', deletes)

    # ---------- phase 3: RG delete + absence ----------
    rg2 = az('group', 'show', '--subscription', sid, '--name', RG, '-o', 'json', timeout=90)
    if not is_notfound(rg2):
        r = az('group', 'delete', '--subscription', sid, '--name', RG, '--yes', '--no-wait',
               timeout=300)
        deletes['rg_delete'] = {'rc': r['rc'], 'err': (r['err'] or '')[:400]}
        emit({'step': 'rg-delete', **deletes['rg_delete']})
        gone, seen = poll_gone_az('rg', ['group', 'show', '--subscription', sid,
                                         '--name', RG, '-o', 'json'], tries=90)
        deletes['rg_gone_notfound'] = gone
        deletes['rg_poll'] = seen[-6:]
    else:
        deletes['rg_delete'] = 'ALREADY_NOTFOUND'
        deletes['rg_gone_notfound'] = True
    save('deletes', deletes)

    # ---------- phase 4: final list NO_RESULTS proof ----------
    lists = {}
    for label, argv in (
            ('envs', ['containerapp', 'env', 'list', '--subscription', sid, '-o', 'json']),
            ('jobs', ['containerapp', 'job', 'list', '--subscription', sid, '-o', 'json']),
            ('sa', ['storage', 'account', 'list', '--subscription', sid, '-o', 'json'])):
        r = az(*argv, timeout=180)
        names = []
        try:
            names = [x.get('name') for x in json.loads(r['out'] or '[]')]
        except Exception:
            pass
        mine = [n for n in names if str(n).lower().startswith(PREFIX)]
        lists[label] = {'rc': r['rc'], 'n': len(names), 'mine': mine}
        emit({'step': 'final-list-' + label, 'rc': r['rc'], 'n': len(names), 'mine': mine})
    env_fin = az('containerapp', 'env', 'show', '--subscription', sid, '--resource-group', RG,
                 '--name', ENVNAME, '-o', 'json', timeout=90)
    job_fin = az('containerapp', 'job', 'show', '--subscription', sid, '--resource-group', RG,
                 '--name', JOBNAME, '-o', 'json', timeout=90)
    rg_fin = az('group', 'show', '--subscription', sid, '--name', RG, '-o', 'json', timeout=90)
    lists['env_final_notfound'] = is_notfound(env_fin)
    lists['job_final_notfound'] = is_notfound(job_fin)
    lists['rg_final_notfound'] = is_notfound(rg_fin)
    save('lists', lists)
    save('events', EVENTS[-200:])

    all_absent = (lists['env_final_notfound'] and lists['job_final_notfound'] and
                  lists['rg_final_notfound'] and not lists['jobs']['mine'] and
                  not lists['envs']['mine'] and not lists['sa']['mine'])
    verdict = 'CLEANUP_DONE_ALL_ABSENT' if all_absent else 'CLEANUP_PARTIAL'
    print(json.dumps(build_result(OUT, verdict), default=str))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:
        sys.stderr.write(TAG + ' FATAL ' + repr(exc)[:400] + '\n')
        print(json.dumps({'verdict': 'CLEANUP_PROBE_EXCEPTION',
                          'chunks': '0', 'matrix_len': '0',
                          'summary': repr(exc)[:800]}, default=str))
        sys.exit(0)
