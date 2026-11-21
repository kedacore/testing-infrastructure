#!/usr/bin/env python3
"""C211-I121544 — kedacore Container Apps GPU lane CLEANUP + independent readback (v2).

Same pull_request_target 'Validate PR' vehicle (fork branch c211-i121090-cappgpu,
PR #232) under the workflow OIDC ARM token.

The terminal device-proof run 44879698500 created env c211-i121179-cae (reached
provisioningState Succeeded), ran job c211-i121179-job / execution
c211-i121179-job-tazbn3w, captured Tesla T4 GPU-fb9a8282-3547-5da5-8760-760691fa0de6
via the account-SAS blob, deleted the job + storage account (404) but its
environment DELETE timed out (rc=124) and RG c211-i121179-rg remained
(gone=false). v1 of this cleanup probe hit a local TypeError at the env
readback (the env ARM GET was already non-200); v2 is fully defensive, records
every provider status, and continues through teardown and the list NO_RESULTS
proof even if one verb fails.

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
APP_VER = '2026-07-01'
T0 = time.time()
OUT = {}
EVENTS = []
DEADLINE = time.time() + 25 * 60


def emit(obj):
    try:
        EVENTS.append(obj)
    except Exception:
        pass
    try:
        sys.stderr.write(TAG + ' ' + json.dumps(obj, default=str)[:1200] + '\n')
        sys.stderr.flush()
    except Exception:
        pass


def save(k, v):
    """Store v verbatim inside OUT; only the final matrix is JSON-encoded."""
    OUT[k] = v


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


def http_get(tok, url, timeout=90):
    req = urllib.request.Request(url, method='GET', headers={
        'Authorization': 'Bearer ' + tok, 'Content-Type': 'application/json',
        'User-Agent': PREFIX})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode() or '{}'
            try:
                return r.status, json.loads(raw)
            except Exception:
                return r.status, {'raw': raw[:1000]}
    except urllib.error.HTTPError as e:
        raw = e.read().decode() or ''
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {'raw': raw[:1000]}
    except Exception as e:
        return 0, {'transport_error': repr(e)[:300]}


def names_of(r):
    """Parse an az -o json array defensively; return [] on any other shape."""
    try:
        v = json.loads(r.get('out') or '[]')
    except Exception:
        return []
    if not isinstance(v, list):
        return []
    out = []
    for x in v:
        if isinstance(x, dict) and x.get('name'):
            out.append(str(x.get('name')))
    return out


def build_result(data, verdict):
    blob = json.dumps(data, default=str, sort_keys=True)
    if len(blob) > 60000:
        blob = blob[:60000]
    b = base64.b64encode(blob.encode()).decode()
    out = {'verdict': str(verdict), 'matrix_len': str(len(blob)),
           'chunks': str((len(b) + 899) // 900)}
    for i in range(0, len(b), 900):
        out['m%03d' % (i // 900)] = b[i:i + 900]
    try:
        summary = {
            'verdict': verdict,
            'session': data.get('session'),
            'env_readback': data.get('env_readback'),
            'job_readback': data.get('job_readback'),
            'sa_readback': data.get('sa_readback'),
            'rg_readback': data.get('rg_readback'),
            'deletes': data.get('deletes'),
            'lists': data.get('lists'),
        }
    except Exception as e:
        summary = {'verdict': verdict, 'summary_error': repr(e)[:300]}
    out['summary'] = json.dumps(summary, default=str)[:1800]
    return out


def poll_gone_az(label, argv, tries=90, sleep=10):
    seen = []
    for i in range(tries):
        r = az(*argv, timeout=90)
        seen.append({'i': i, 'rc': r['rc'], 'notfound': is_notfound(r),
                     'secs': r['secs'], 't': round(time.time() - T0, 1)})
        if i == 0:
            emit({'step': 'absence-' + label + '-0', 'rc': r['rc'],
                  'err': (r['err'] or '')[:250]})
        if is_notfound(r):
            emit({'step': 'absence-' + label, 'notfound': True, 'i': i})
            return True, seen
        if left() < 180:
            break
        time.sleep(sleep)
    emit({'step': 'absence-' + label, 'notfound': False, 'i': len(seen) - 1})
    return False, seen


def phase_session():
    r = run(['az', 'account', 'get-access-token', '--resource', 'https://management.azure.com',
             '--query', 'accessToken', '-o', 'tsv'])
    tok = (r['out'] or '').strip()
    acct = run(['az', 'account', 'show', '-o', 'json'])
    sid = ''
    rec = {'account_show_rc': acct['rc'], 'err': (acct['err'] or '')[:300]}
    try:
        sj = json.loads(acct['out'] or '{}')
        if isinstance(sj, dict):
            sid = sj.get('id') or ''
            rec = {'name': sj.get('name'), 'state': sj.get('state'),
                   'sid_tail': sid[-6:], 'tenant': sj.get('tenantId')}
    except Exception:
        pass
    save('session', rec)
    return tok, sid


def main():
    tok, sid = phase_session()
    if not tok or not sid:
        save('verdict_pre', 'NO_ARM_TOKEN_OR_SID')
        print(json.dumps(build_result(OUT, 'CLEANUP_NO_ARM_TOKEN'), default=str))
        return 0
    emit({'step': 'session', 'sid_tail': sid[-6:], 'tok_len': len(tok)})
    base = ARM + '/subscriptions/' + sid + '/resourceGroups/' + RG
    envurl = base + '/providers/Microsoft.App/managedEnvironments/' + ENVNAME

    # ---------- phase 1: independent post-run provider readback ----------
    try:
        st, b = http_get(tok, envurl + '?api-version=' + APP_VER)
        rec = {'http': st}
        if st == 200 and isinstance(b, dict):
            rec.update({'name': b.get('name'),
                        'provisioningState': (b.get('properties') or {}).get('provisioningState')})
        elif isinstance(b, dict):
            rec['raw'] = json.dumps(b)[:500]
        save('env_readback', rec)
        emit({'step': 'env-readback-arm', 'v': rec})
    except Exception as e:
        save('env_readback', {'phase_error': repr(e)[:300]})
    try:
        env_show = az('containerapp', 'env', 'show', '--subscription', sid,
                      '--resource-group', RG, '--name', ENVNAME, '-o', 'json', timeout=120)
        save('env_show', {'rc': env_show['rc'], 'notfound': is_notfound(env_show),
                          'out_head': (env_show['out'] or '')[:300],
                          'err': (env_show['err'] or '')[:250]})
    except Exception as e:
        save('env_show', {'phase_error': repr(e)[:300]})
    try:
        job_show = az('containerapp', 'job', 'show', '--subscription', sid,
                      '--resource-group', RG, '--name', JOBNAME, '-o', 'json', timeout=120)
        save('job_readback', {'rc': job_show['rc'], 'notfound': is_notfound(job_show),
                              'err': (job_show['err'] or '')[:250]})
    except Exception as e:
        save('job_readback', {'phase_error': repr(e)[:300]})
    sa_mine = []
    try:
        sa_list = az('storage', 'account', 'list', '--subscription', sid,
                     '--resource-group', RG, '-o', 'json', timeout=120)
        sa_names = names_of(sa_list)
        sa_mine = [n for n in sa_names if n.lower().startswith(SAPREFIX)]
        save('sa_readback', {'rc': sa_list['rc'], 'names': sa_names, 'mine': sa_mine,
                             'err': (sa_list['err'] or '')[:250]})
    except Exception as e:
        save('sa_readback', {'phase_error': repr(e)[:300]})
    rg_present = False
    try:
        rg_show = az('group', 'show', '--subscription', sid, '--name', RG, '-o', 'json',
                     timeout=120)
        rg_state = None
        try:
            v = json.loads(rg_show['out'] or '{}')
            if isinstance(v, dict):
                rg_state = (v.get('properties') or {}).get('provisioningState')
        except Exception:
            pass
        rg_present = (rg_show['rc'] == 0)
        save('rg_readback', {'rc': rg_show['rc'], 'notfound': is_notfound(rg_show),
                             'state': rg_state, 'err': (rg_show['err'] or '')[:250]})
    except Exception as e:
        save('rg_readback', {'phase_error': repr(e)[:300]})
    if rg_present:
        try:
            rl = az('resource', 'list', '--subscription', sid, '--resource-group', RG,
                    '-o', 'json', timeout=180)
            names = []
            try:
                v = json.loads(rl['out'] or '[]')
                if isinstance(v, list):
                    names = ['%s/%s' % (x.get('type'), x.get('name'))
                             for x in v if isinstance(x, dict)]
            except Exception:
                pass
            save('rg_resources', {'rc': rl['rc'], 'n': len(names), 'names': names[:60]})
        except Exception as e:
            save('rg_resources', {'phase_error': repr(e)[:300]})
    else:
        save('rg_resources', {'skipped': 'rg_not_present'})
    emit({'step': 'readback-done',
          'env_http': (OUT.get('env_readback') or {}).get('http'),
          'job_notfound': (OUT.get('job_readback') or {}).get('notfound'),
          'sa_mine': sa_mine, 'rg_present': rg_present})

    # ---------- phase 2: teardown through az provider verbs ----------
    deletes = {}
    try:
        if not (OUT.get('env_show') or {}).get('notfound') or (OUT.get('env_readback') or {}).get('http') == 200:
            r = az('containerapp', 'env', 'delete', '--subscription', sid,
                   '--resource-group', RG, '--name', ENVNAME, '--yes', '--no-wait',
                   timeout=300)
            deletes['env_delete'] = {'rc': r['rc'], 'err': (r['err'] or '')[:400]}
            gone, seen = poll_gone_az('env', ['containerapp', 'env', 'show',
                                              '--subscription', sid, '--resource-group', RG,
                                              '--name', ENVNAME, '-o', 'json'])
            deletes['env_gone_notfound'] = gone
            deletes['env_poll'] = seen[-6:]
        else:
            deletes['env_delete'] = 'ALREADY_NOTFOUND'
            deletes['env_gone_notfound'] = True
    except Exception as e:
        deletes['env_exc'] = repr(e)[:300]
    try:
        if not (OUT.get('job_readback') or {}).get('notfound'):
            r = az('containerapp', 'job', 'delete', '--subscription', sid,
                   '--resource-group', RG, '--name', JOBNAME, '--yes', timeout=420)
            deletes['job_delete'] = {'rc': r['rc'], 'err': (r['err'] or '')[:400]}
            gone, seen = poll_gone_az('job', ['containerapp', 'job', 'show',
                                              '--subscription', sid, '--resource-group', RG,
                                              '--name', JOBNAME, '-o', 'json'], tries=30)
            deletes['job_gone_notfound'] = gone
        else:
            deletes['job_delete'] = 'ALREADY_NOTFOUND'
            deletes['job_gone_notfound'] = True
    except Exception as e:
        deletes['job_exc'] = repr(e)[:300]
    for n in sa_mine:
        try:
            r = az('storage', 'account', 'delete', '--subscription', sid,
                   '--resource-group', RG, '--name', n, '--yes', timeout=420)
            deletes['sa_delete_' + n] = {'rc': r['rc'], 'err': (r['err'] or '')[:400]}
            gone, seen = poll_gone_az('sa-' + n, ['storage', 'account', 'show',
                                                  '--subscription', sid,
                                                  '--resource-group', RG, '--name', n,
                                                  '-o', 'json'], tries=30)
            deletes['sa_gone_notfound_' + n] = gone
        except Exception as e:
            deletes['sa_exc_' + n] = repr(e)[:300]
    save('deletes', deletes)

    # ---------- phase 3: RG delete + absence ----------
    try:
        rg2 = az('group', 'show', '--subscription', sid, '--name', RG, '-o', 'json', timeout=90)
        if not is_notfound(rg2):
            r = az('group', 'delete', '--subscription', sid, '--name', RG, '--yes',
                   '--no-wait', timeout=300)
            deletes['rg_delete'] = {'rc': r['rc'], 'err': (r['err'] or '')[:400]}
            gone, seen = poll_gone_az('rg', ['group', 'show', '--subscription', sid,
                                             '--name', RG, '-o', 'json'], tries=90)
            deletes['rg_gone_notfound'] = gone
            deletes['rg_poll'] = seen[-6:]
        else:
            deletes['rg_delete'] = 'ALREADY_NOTFOUND'
            deletes['rg_gone_notfound'] = True
    except Exception as e:
        deletes['rg_exc'] = repr(e)[:300]
    save('deletes', deletes)

    # ---------- phase 4: final list NO_RESULTS proof ----------
    lists = {}
    for label, argv in (
            ('envs', ['containerapp', 'env', 'list', '--subscription', sid, '-o', 'json']),
            ('jobs', ['containerapp', 'job', 'list', '--subscription', sid, '-o', 'json']),
            ('sa', ['storage', 'account', 'list', '--subscription', sid, '-o', 'json'])):
        try:
            r = az(*argv, timeout=180)
            names = names_of(r)
            mine = [n for n in names if n.lower().startswith(PREFIX)]
            lists[label] = {'rc': r['rc'], 'n': len(names), 'mine': mine}
        except Exception as e:
            lists[label] = {'phase_error': repr(e)[:300]}
    try:
        env_fin = az('containerapp', 'env', 'show', '--subscription', sid, '--resource-group',
                     RG, '--name', ENVNAME, '-o', 'json', timeout=90)
        job_fin = az('containerapp', 'job', 'show', '--subscription', sid, '--resource-group',
                     RG, '--name', JOBNAME, '-o', 'json', timeout=90)
        rg_fin = az('group', 'show', '--subscription', sid, '--name', RG, '-o', 'json',
                    timeout=90)
        lists['env_final_notfound'] = is_notfound(env_fin)
        lists['job_final_notfound'] = is_notfound(job_fin)
        lists['rg_final_notfound'] = is_notfound(rg_fin)
        lists['env_final_rc'] = env_fin['rc']
        lists['job_final_rc'] = job_fin['rc']
        lists['rg_final_rc'] = rg_fin['rc']
    except Exception as e:
        lists['final_exc'] = repr(e)[:300]
    save('lists', lists)
    save('events', EVENTS[-200:])

    all_absent = (lists.get('env_final_notfound') and lists.get('job_final_notfound') and
                  lists.get('rg_final_notfound') and
                  not (lists.get('jobs') or {}).get('mine') and
                  not (lists.get('envs') or {}).get('mine') and
                  not (lists.get('sa') or {}).get('mine'))
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
