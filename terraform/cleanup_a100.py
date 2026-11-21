#!/usr/bin/env python3
"""C211-I121663 — resource-free cleanup verification for the A100 CApps lane.

Creates NOTHING. Re-GETs our own nonce resources, re-issues az CLI deletes if any
survive, polls to 404, and lists the subscription/RG for c211-i121663* leftovers.
Emits tagged JSON on stderr and a flat terraform external map on stdout.
"""
import base64
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request

ARM = 'https://management.azure.com'
TAG = 'C211_I121663_CLEANUP'
PREFIX = 'c211-i121663'
RG = PREFIX + '-rg'
APP_VER = '2026-07-01'
STG_VER = '2023-01-01'
EVENTS = []


def emit(obj):
    EVENTS.append(obj)
    sys.stderr.write(TAG + ' ' + json.dumps(obj, default=str)[:2000] + '\n')
    sys.stderr.flush()


def run(cmd, timeout=300):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {'rc': p.returncode, 'out': p.stdout or '', 'err': (p.stderr or '')[:400]}
    except subprocess.TimeoutExpired:
        return {'rc': 124, 'out': '', 'err': 'TIMEOUT'}
    except Exception as e:  # noqa: BLE001
        return {'rc': -1, 'out': '', 'err': repr(e)[:300]}


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
                    'message': (e.get('message') or '')[:600]}
        if 'raw' in body:
            return {'http': st, 'raw': body['raw'][:600]}
        return {'http': st, 'body': json.dumps(body)[:600]}
    return {'http': st}


def main():
    r = az('account', 'get-access-token', '--resource', ARM, '--query', 'accessToken',
           '-o', 'tsv')
    tok = (r['out'] or '').strip()
    acc = {}
    r2 = az('account', 'show', '-o', 'json')
    if r2['rc'] == 0:
        try:
            acc = json.loads(r2['out'])
        except Exception:  # noqa: BLE001
            pass
    sid = acc.get('id') or ''
    emit({'step': 'session', 'sub': acc.get('name'), 'sid_tail': sid[-6:],
          'token': bool(tok)})
    if not sid or not tok:
        print(json.dumps({'verdict': 'TOKEN_FAILED'}))
        return 0
    out = {}
    # ---- environments ----
    envg = http(tok, ARM + '/subscriptions/' + sid + '/resourceGroups/' + RG +
                '/providers/Microsoft.App/managedEnvironments/' + PREFIX + '-cae?api-version='
                + APP_VER)
    emit({'step': 'env-get', 'http': envg[0],
          'state': ((envg[1].get('properties') or {}).get('provisioningState')
                    if isinstance(envg[1], dict) else None)})
    if envg[0] == 200:
        az('containerapp', 'env', 'delete', '--subscription', sid, '--resource-group', RG,
           '--name', PREFIX + '-cae', '--yes', '--no-wait', timeout=240)
        for i in range(90):
            st, b = http(tok, ARM + '/subscriptions/' + sid + '/resourceGroups/' + RG +
                         '/providers/Microsoft.App/managedEnvironments/' + PREFIX +
                         '-cae?api-version=' + APP_VER)
            emit({'step': 'env-absence-poll', 'i': i, 'http': st,
                  'state': ((b.get('properties') or {}).get('provisioningState')
                            if isinstance(b, dict) else None)})
            if st == 404:
                break
            time.sleep(10)
    st, b = http(tok, ARM + '/subscriptions/' + sid + '/resourceGroups/' + RG +
                 '/providers/Microsoft.App/managedEnvironments/' + PREFIX +
                 '-cae?api-version=' + APP_VER)
    out['env_final_http'] = st
    emit({'step': 'env-final', 'http': st})
    # ---- jobs + executions ----
    st, b = http(tok, ARM + '/subscriptions/' + sid + '/resourceGroups/' + RG +
                 '/providers/Microsoft.App/jobs/' + PREFIX + '-job?api-version=' + APP_VER)
    out['job_final_http'] = st
    emit({'step': 'job-final', 'http': st})
    st, b = http(tok, ARM + '/subscriptions/' + sid + '/resourceGroups/' + RG +
                 '/providers/Microsoft.App/jobs/' + PREFIX + '-job/executions?api-version='
                 + APP_VER)
    emit({'step': 'executions-list', 'http': st,
          'n': len((b.get('value') or []) if isinstance(b, dict) else [])})
    out['executions_list_http'] = st
    # ---- storage accounts + RGs + lists ----
    st, b = http(tok, ARM + '/subscriptions/' + sid + '/providers/Microsoft.Storage/'
                 'storageAccounts?api-version=' + STG_VER)
    names = [x.get('name') for x in (b.get('value') or [])] if isinstance(b, dict) else []
    mine_sa = [n for n in names if str(n).lower().startswith('c211i121663')]
    out['sa_list_mine'] = mine_sa
    emit({'step': 'sa-list', 'http': st, 'mine': mine_sa})
    for sa in mine_sa:
        az('storage', 'account', 'delete', '--subscription', sid, '--resource-group', RG,
           '--name', sa, '--yes', timeout=420)
    st, b = http(tok, ARM + '/subscriptions/' + sid + '/providers/Microsoft.App/'
                 'managedEnvironments?api-version=' + APP_VER)
    names = [x.get('name') for x in (b.get('value') or [])] if isinstance(b, dict) else []
    out['env_list_mine'] = [n for n in names if str(n).lower().startswith(PREFIX)]
    emit({'step': 'env-list', 'http': st, 'mine': out['env_list_mine']})
    st, b = http(tok, ARM + '/subscriptions/' + sid + '/providers/Microsoft.App/jobs'
                 '?api-version=' + APP_VER)
    names = [x.get('name') for x in (b.get('value') or [])] if isinstance(b, dict) else []
    out['job_list_mine'] = [n for n in names if str(n).lower().startswith(PREFIX)]
    emit({'step': 'job-list', 'http': st, 'mine': out['job_list_mine']})
    st, b = http(tok, ARM + '/subscriptions/' + sid + '/resourceGroups/' + RG +
                 '?api-version=2021-04-01')
    out['rg_final_http'] = st
    emit({'step': 'rg-final', 'http': st,
          'state': (b.get('properties') or {}).get('provisioningState')
          if isinstance(b, dict) else None})
    if st == 200:
        az('group', 'delete', '--subscription', sid, '--name', RG, '--yes', '--no-wait',
           timeout=300)
        for i in range(60):
            r3 = az('group', 'show', '--subscription', sid, '--name', RG, '-o', 'json',
                    timeout=90)
            if is_notfound(r3):
                out['rg_gone'] = True
                break
            time.sleep(10)
        out.setdefault('rg_gone', False)
        emit({'step': 'rg-absence', 'notfound': out['rg_gone']})
    else:
        out['rg_gone'] = True
    out['verdict'] = ('CLEAN_CONFIRMED'
                      if out.get('env_final_http') == 404 and out.get('job_final_http') == 404
                      and not out.get('env_list_mine') and not out.get('job_list_mine')
                      and not out.get('sa_list_mine') and out.get('rg_gone')
                      else 'CLEANUP_INCOMPLETE')
    emit({'step': 'DONE', 'verdict': out['verdict'], 'matrix': out})
    # terraform external contract: flat string map; chunk the matrix base64.
    b64 = base64.b64encode(json.dumps(out, default=str).encode()).decode()
    CH = 30000
    parts = [b64[i:i + CH] for i in range(0, len(b64), CH)] or ['']
    flat = {'verdict': out['verdict'], 'chunks': str(len(parts)),
            'matrix_len': str(len(json.dumps(out, default=str)))}
    for i, part in enumerate(parts):
        flat['m%03d' % i] = part
    print(json.dumps(flat))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        emit({'step': 'FATAL', 'error': repr(exc)[:400]})
        print(json.dumps({'verdict': 'CLEANUP_EXCEPTION'}))
        sys.exit(0)
